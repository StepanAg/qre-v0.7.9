"""Look-ahead protection, MTF alignment, persistence, cache, research safety."""
import ast
import shutil
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from app.core.errors import StorageError
from app.domain.enums import FeatureStatus, QualityStatus
from app.domain.market import Timeframe
from app.research.alignment import is_known_at, last_closed_open_time
from app.research.engine import FeatureEngine
from app.research.service import FeatureService
from app.storage.database import MIGRATIONS_DIR, bootstrap, connect, migrate
from app.storage.features import SQLiteFeatureSnapshotStore
from app.storage.market_data import SQLiteMarketDataStore
from app.storage.writer import SerializedWriter
from tests.feature_data import BTC, T0, bar, end_of, poisoned, walk

M15, H1, H4 = Timeframe.M15, Timeframe.H1, Timeframe.H4
E = FeatureEngine()


class LookAheadTests(unittest.TestCase):  # Test 9
    def test_future_bars_do_not_change_past_features(self):
        cs = walk(1200)
        t = cs[999].close_time
        past = E.snapshot(cs[:1000], M15, t, symbol=BTC)
        with_future = E.snapshot(cs, M15, t, symbol=BTC)
        self.assertEqual(past.features, with_future.features)
        self.assertEqual(past.input_fingerprint, with_future.input_fingerprint)

    def test_poisoned_future_is_invisible(self):
        cs = walk(1200)
        t = cs[999].close_time
        poisoned_future = cs[:1000] + [poisoned(c) for c in cs[1000:]]
        self.assertEqual(E.snapshot(cs, M15, t, symbol=BTC).features,
                         E.snapshot(poisoned_future, M15, t, symbol=BTC).features)

    def test_changing_the_decision_bar_does_change_value(self):
        """Control: the test above would be vacuous if features ignored data."""
        cs = walk(1200)
        t = cs[999].close_time
        altered = cs[:999] + [poisoned(cs[999])] + cs[1000:]
        self.assertNotEqual(E.snapshot(cs, M15, t, ["ret_cc"], symbol=BTC).features,
                            E.snapshot(altered, M15, t, ["ret_cc"], symbol=BTC).features)

    def test_historical_calculator_equals_point_in_time(self):
        cs = walk(1000)
        names = ["ema_50", "atr_14", "rel_volume_20", "range_position_20"]
        report = E.coverage(cs, M15, names)
        full = E.policy.prepare(cs, M15, end_of(cs))
        for idx in (60, 250, 999):
            t = cs[idx].close_time
            direct = E.snapshot(cs[:idx + 1], M15, t, names, symbol=BTC)
            view = full.upto(t)
            for n in names:
                self.assertEqual(E.evaluate(view, E.registry.get(n)), direct.features[n], (idx, n))
        self.assertEqual(report.points, 1000)

    def test_no_feature_reads_beyond_window(self):
        """Every formula is called with exactly min_observations bars ending at T."""
        seen = {}
        eng = FeatureEngine()
        orig = eng.evaluate

        def spy(series, spec):
            f = orig(series, spec)
            if f.status is FeatureStatus.VALUE:
                seen[spec.name] = (f.bars_used, spec.min_observations)
            return f
        eng.evaluate = spy
        cs = walk(1000)
        eng.snapshot(cs, M15, end_of(cs), symbol=BTC)
        for name, (used, need) in seen.items():
            self.assertEqual(used, need, name)


class MTFAlignmentTests(unittest.TestCase):  # Test 11
    def setUp(self):
        self.m15 = walk(4 * 24 * 10, M15)
        self.h1 = walk(24 * 10, H1, seed=11)
        self.h4 = walk(6 * 10, H4, seed=12)

    def _snap(self, as_of, h1=None):
        return E.snapshot_mtf({M15: self.m15, H1: h1 or self.h1, H4: self.h4}, M15, as_of,
                              {M15: ["ret_oc"], H1: ["ret_oc"], H4: ["ret_oc"]}, symbol=BTC)

    def _ret_oc(self, c):
        return float(c.close) / float(c.open) - 1

    def test_alignment_helpers(self):
        t = T0 + timedelta(days=5, hours=15, minutes=15)
        self.assertEqual(last_closed_open_time(H1, t), T0 + timedelta(days=5, hours=14))
        self.assertEqual(last_closed_open_time(H4, t), T0 + timedelta(days=5, hours=8))
        self.assertFalse(is_known_at(T0 + timedelta(days=5, hours=15), H1, t))
        self.assertTrue(is_known_at(T0 + timedelta(days=5, hours=16), H1, T0 + timedelta(days=5, hours=17)))

    def test_1515_uses_1400_hour_bar_not_forming_1500(self):
        t = T0 + timedelta(days=5, hours=15, minutes=15)
        snap = self._snap(t)
        by_time = {c.open_time: c for c in self.h1}
        expected = self._ret_oc(by_time[T0 + timedelta(days=5, hours=14)])
        self.assertAlmostEqual(snap.get("1h:ret_oc"), expected, places=12)
        forming = self._ret_oc(by_time[T0 + timedelta(days=5, hours=15)])
        self.assertNotAlmostEqual(snap.get("1h:ret_oc"), forming, places=12)

    def test_1600_now_uses_1500_bar(self):
        t = T0 + timedelta(days=5, hours=16)
        by_time = {c.open_time: c for c in self.h1}
        self.assertAlmostEqual(self._snap(t).get("1h:ret_oc"),
                               self._ret_oc(by_time[T0 + timedelta(days=5, hours=15)]), places=12)

    def test_4h_uses_last_closed_4h_bar(self):
        t = T0 + timedelta(days=5, hours=15, minutes=15)
        by_time = {c.open_time: c for c in self.h4}
        self.assertAlmostEqual(self._snap(t).get("4h:ret_oc"),
                               self._ret_oc(by_time[T0 + timedelta(days=5, hours=8)]), places=12)

    def test_forming_higher_bar_poisoned_is_invisible(self):
        t = T0 + timedelta(days=5, hours=15, minutes=15)
        target = T0 + timedelta(days=5, hours=15)
        h1 = [poisoned(c) if c.open_time >= target else c for c in self.h1]
        self.assertEqual(self._snap(t).features, self._snap(t, h1).features)

    def test_missing_higher_bar_is_stale_not_older_bar(self):
        t = T0 + timedelta(days=5, hours=15, minutes=15)
        h1 = [c for c in self.h1 if c.open_time != T0 + timedelta(days=5, hours=14)]
        f = self._snap(t, h1).features["1h:ret_oc"]
        self.assertEqual((f.status, f.quality), (FeatureStatus.DATA_QUALITY_FAILURE, QualityStatus.STALE))


class _DB:
    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="qre_feat_"))
        self.conn = bootstrap(self.dir / "f.sqlite3", shared=True)
        self.writer = SerializedWriter(self.conn)
        self.md = SQLiteMarketDataStore(self.writer)
        self.fs = SQLiteFeatureSnapshotStore(self.writer)

    def close(self):
        self.conn.close()
        shutil.rmtree(self.dir, ignore_errors=True)


class FeaturePersistenceTests(unittest.TestCase):  # Test 15
    def setUp(self):
        self.db = _DB()

    def tearDown(self):
        self.db.close()

    def test_roundtrip_exact(self):
        cs = walk(1000, missing={990})
        snap = E.snapshot(cs, M15, end_of(cs), symbol=BTC)
        self.assertTrue(self.db.fs.save(snap))
        back = self.db.fs.load(BTC, M15, snap.as_of, snap.feature_set_hash, snap.input_fingerprint)
        self.assertEqual(back, snap)
        for k, f in snap.features.items():
            if f.value is not None:
                self.assertEqual(back.features[k].value.hex(), f.value.hex(), k)
        self.assertTrue(any(f.status is not FeatureStatus.VALUE for f in back.features.values()))

    def test_idempotent_and_nondeterminism_detected(self):
        cs = walk(300)
        snap = E.snapshot(cs, M15, end_of(cs), ["sma_20"], symbol=BTC)
        self.assertTrue(self.db.fs.save(snap))
        self.assertFalse(self.db.fs.save(snap))
        f = snap.features["sma_20"]
        tampered = replace(snap, features={"sma_20": replace(f, value=f.value + 1)})
        with self.assertRaises(StorageError):
            self.db.fs.save(tampered)

    def test_db_constraints(self):
        cs = walk(30)
        snap = E.snapshot(cs, M15, end_of(cs), ["sma_20"], symbol=BTC)
        self.db.fs.save(snap)
        sid = self.db.conn.execute("SELECT snapshot_id FROM feature_snapshots").fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):   # value without status 'value'
            self.db.conn.execute("INSERT INTO feature_values VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (sid, "x", "x", "15m", 1, "invalid", 1.0, "u", 1, 1, 1, "valid", "no_gap", "r"))
        with self.assertRaises(sqlite3.IntegrityError):   # NaN is stored as NULL -> rejected for 'value'
            self.db.conn.execute("INSERT INTO feature_values VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (sid, "y", "y", "15m", 1, "value", float("nan"), "u", 1, 1, 1, "valid", "no_gap", None))
        with self.assertRaises(sqlite3.DatabaseError):
            self.db.conn.execute("UPDATE feature_values SET value=2")

    def test_migration_chain_and_upgrade(self):
        vs = [r[0] for r in self.db.conn.execute("SELECT version FROM schema_migrations ORDER BY 1")]
        # forward-compatible: later phases append migrations after 0003
        self.assertEqual(vs[:3], ["0001_initial", "0002_market_data", "0003_features"])
        self.assertEqual(vs, sorted(vs))
        d = self.db.dir / "m"
        d.mkdir()
        for f in ("0001_initial.sql", "0002_market_data.sql"):
            shutil.copy(MIGRATIONS_DIR / f, d)
        c = connect(self.db.dir / "old.sqlite3")
        migrate(c, d)
        self.assertEqual(migrate(c)[0], "0003_features")
        c.close()


class FeatureServiceCacheTests(unittest.TestCase):
    def setUp(self):
        self.db = _DB()

    def tearDown(self):
        self.db.close()

    def test_loads_from_store_and_caches(self):
        cs = walk(1000)
        self.db.md.upsert_candles(cs, "t")
        svc = FeatureService(self.db.md, cache=self.db.fs)
        t = end_of(cs) + timedelta(minutes=3)
        a = svc.snapshot(BTC, t, {M15: ["ema_200", "atr_14"]}, M15)
        self.assertEqual(a.get("ema_200"), E.snapshot(cs, M15, t, ["ema_200"], symbol=BTC).get("ema_200"))
        b = svc.snapshot(BTC, t, {M15: ["ema_200", "atr_14"]}, M15)
        self.assertEqual((svc.cache_misses, svc.cache_hits), (1, 1))
        self.assertEqual(a, b)

    def test_stale_store_reported(self):
        cs = walk(300)
        self.db.md.upsert_candles(cs, "t")
        f = FeatureService(self.db.md).snapshot(BTC, end_of(cs) + timedelta(hours=1), {M15: ["sma_20"]}, M15)
        self.assertEqual(f.features["sma_20"].quality, QualityStatus.STALE)

    def test_changed_data_changes_cache_key(self):
        cs = walk(300, missing={290})
        self.db.md.upsert_candles(cs, "t")
        svc = FeatureService(self.db.md, cache=self.db.fs)
        t = end_of(cs)
        first = svc.snapshot(BTC, t, {M15: ["sma_20"]}, M15)
        self.assertEqual(first.features["sma_20"].status, FeatureStatus.DATA_QUALITY_FAILURE)
        self.db.md.upsert_candles([c for c in walk(300) if c.open_time == T0 + M15.delta * 290], "t")  # gap repaired
        second = svc.snapshot(BTC, t, {M15: ["sma_20"]}, M15)
        self.assertEqual(second.features["sma_20"].status, FeatureStatus.VALUE)
        self.assertEqual(svc.cache_hits, 0)


class ResearchSafetyTests(unittest.TestCase):
    ALLOWED = ("app.domain", "app.research", "app.core", "app.data.ports", "app.data.quality")

    def test_research_imports_only_allowed_layers(self):
        root = Path(__file__).resolve().parents[1] / "app" / "research"
        bad = []
        for f in root.rglob("*.py"):
            for node in ast.walk(ast.parse(f.read_text())):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    mods = [node.module]
                for m in mods:
                    if m.startswith("app.") and not m.startswith(self.ALLOWED):
                        bad.append(f"{f.name}: {m}")
        self.assertEqual(bad, [], "research may not import strategy/execution/ai/storage/network/cli")

    def test_no_forbidden_capabilities_in_research(self):
        """Checks identifiers actually used in code (not docstrings/comments)."""
        root = Path(__file__).resolve().parents[1] / "app" / "research"
        forbidden = {"submit", "place_order", "sleep", "urlopen", "telegram", "decide", "api_key", "cancel"}
        hits = []
        for f in root.rglob("*.py"):
            for node in ast.walk(ast.parse(f.read_text())):
                ident = (node.id if isinstance(node, ast.Name) else
                         node.attr if isinstance(node, ast.Attribute) else None)
                if ident and ident.lower() in forbidden:
                    hits.append(f"{f.name}:{node.lineno}:{ident}")
        self.assertEqual(hits, [])
