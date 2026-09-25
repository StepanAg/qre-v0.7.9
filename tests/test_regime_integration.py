"""Regime Engine end-to-end on synthetic candles via the real Feature Engine and SQLite."""
import contextlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest import mock

from app.core.errors import StorageError
from app.domain.enums import (ContextStatus, MarketRegime, MtfAlignment, QualityStatus, TrendDirection,
                              VolatilityState)
from app.domain.regime import RegimeSnapshot
from app.research.regime.config import RegimeConfig
from app.research.regime.service import RegimeService
from app.research.service import FeatureService
from app.storage.regime import SQLiteRegimeSnapshotStore
from tests.fakes import TempDB
from tests.feature_data import poisoned
from tests.regime_data import AS_OF, BTC, CONFIG_FILE, ETH, H1, H4, M15, cfg, series


class World:
    """Temp DB with candles; builds the service exactly like the CLI does."""

    def __init__(self, kinds=None, *, symbol=ETH, btc="up", extra=(), **series_kw):
        self.db = TempDB()
        kinds = kinds if kinds is not None else {M15: "up", H1: "up", H4: "up"}
        for tf, kind in kinds.items():
            self.db.store.upsert_candles(series(kind, tf, symbol=symbol, **series_kw), "t")
        if btc:
            self.db.store.upsert_candles(series(btc, H1, symbol=BTC), "t")
        for batch in extra:
            self.db.store.upsert_candles(batch, "t")
        self.store = SQLiteRegimeSnapshotStore(self.db.writer)
        self.svc = RegimeService(FeatureService(self.db.store), cfg(), self.store)

    def analyze(self, symbol=ETH, as_of=AS_OF, base=M15, tfs=None, **kw):
        return self.svc.analyze(symbol, as_of, base, tfs, **kw)

    def close(self):
        self.db.close()


def world(*a, **kw):
    w = World(*a, **kw)
    return w


class ScenarioTests(unittest.TestCase):
    def _run(self, kind):
        w = world({M15: kind, H1: kind, H4: kind})
        try:
            return w.analyze()
        finally:
            w.close()

    def test_sustained_uptrend(self):
        r = self._run("up")
        self.assertEqual((r.regime, r.trend_direction, r.mtf_alignment),
                         (MarketRegime.TRENDING_UP, TrendDirection.UP, MtfAlignment.ALIGNED))
        self.assertEqual(r.data_quality, QualityStatus.VALID)

    def test_sustained_downtrend(self):
        r = self._run("down")
        self.assertEqual((r.regime, r.trend_direction), (MarketRegime.TRENDING_DOWN, TrendDirection.DOWN))

    def test_ranging(self):
        self.assertEqual(self._run("range").regime, MarketRegime.RANGING)

    def test_high_volatility(self):
        r = self._run("shock")
        self.assertEqual((r.regime, r.volatility_state), (MarketRegime.HIGH_VOLATILITY, VolatilityState.HIGH))

    def test_transition(self):
        r = self._run("turn_down")
        self.assertEqual((r.regime, r.trend_direction), (MarketRegime.TRANSITION, TrendDirection.CONFLICT))


class RegimeDataFailureTests(unittest.TestCase):
    def test_insufficient_history(self):
        w = world(n=300)
        try:
            r = w.analyze()
            self.assertEqual((r.regime, r.data_quality), (MarketRegime.UNKNOWN, QualityStatus.INSUFFICIENT_HISTORY))
            self.assertTrue(any(c.startswith("REQ_FEATURE_UNAVAILABLE:15m:ema_50_200_spread") for c in r.reason_codes))
        finally:
            w.close()

    def test_stale_data(self):
        w = world()
        try:
            r = w.analyze(as_of=AS_OF + M15.delta * 3)     # 3 closed bars are missing from the store
            self.assertEqual((r.regime, r.data_quality), (MarketRegime.UNKNOWN, QualityStatus.STALE))
            self.assertIn("UNKNOWN:15m:DATA_QUALITY_STALE", r.reason_codes)
        finally:
            w.close()

    def test_gap_inside_window(self):
        w = world({M15: "up", H1: "up", H4: "up"}, missing={880})
        try:
            r = w.analyze(tfs=(M15,))
            self.assertEqual((r.regime, r.data_quality), (MarketRegime.UNKNOWN, QualityStatus.GAP))
        finally:
            w.close()

    def test_no_data_at_all(self):
        w = world({}, btc=None)
        try:
            r = w.analyze()
            self.assertEqual(r.regime, MarketRegime.UNKNOWN)
            self.assertEqual(r.data_quality, QualityStatus.MISSING_HISTORY)
            self.assertEqual(r.btc_context.status, ContextStatus.UNAVAILABLE)
        finally:
            w.close()


class MultiTimeframeTests(unittest.TestCase):
    def test_conflicting_timeframes_are_reported_not_hidden(self):
        w = world({M15: "down", H1: "up", H4: "up"})
        try:
            r = w.analyze()
            self.assertEqual(r.regime, MarketRegime.TRENDING_DOWN)          # base tf result unchanged
            self.assertEqual(r.mtf_alignment, MtfAlignment.CONFLICT)
            self.assertTrue(any(c.startswith("MTF_CONFLICT:15m=trending_down,1h=trending_up,4h=trending_up")
                                for c in r.reason_codes))
            self.assertEqual([t.regime for t in r.timeframes],
                             [MarketRegime.TRENDING_DOWN, MarketRegime.TRENDING_UP, MarketRegime.TRENDING_UP])
        finally:
            w.close()

    def test_higher_timeframe_unavailable_is_partial(self):
        w = world({M15: "up", H1: "up", H4: "up"})
        w.close()
        w = world({M15: "up", H1: "up"})                                  # no 4h data
        try:
            r = w.analyze()
            self.assertEqual((r.regime, r.mtf_alignment), (MarketRegime.TRENDING_UP, MtfAlignment.PARTIAL))
            self.assertTrue(any(c.startswith("MTF_UNAVAILABLE:4h") for c in r.reason_codes))
        finally:
            w.close()

    def test_feature_timestamps_reflect_last_closed_bar_per_tf(self):
        as_of = AS_OF + timedelta(hours=1, minutes=30)                   # 13:30
        w = world({}, btc=None, extra=[series("up", tf, end=tf.floor(as_of)) for tf in (M15, H1, H4)])
        try:
            r = w.analyze(as_of=as_of)
            self.assertEqual(r.feature_timestamps, {"15m": "2026-03-02T13:30:00+00:00",
                                                    "1h": "2026-03-02T13:00:00+00:00",
                                                    "4h": "2026-03-02T12:00:00+00:00"})
            self.assertEqual(r.mtf_alignment, MtfAlignment.ALIGNED)       # 4h is not "stale" at 13:30
        finally:
            w.close()


class RegimeLookAheadTests(unittest.TestCase):
    def test_future_and_forming_bars_do_not_change_snapshot(self):
        base = world()
        try:
            ref = base.analyze().to_json()
        finally:
            base.close()
        fut15 = [poisoned(c) for c in series("up", M15, end=AS_OF + M15.delta * 20, n=20)]
        forming_1h = [poisoned(c) for c in series("up", H1, end=AS_OF + H1.delta, n=1)]   # 12:00-13:00
        forming_4h = [poisoned(c) for c in series("up", H4, end=AS_OF + H4.delta, n=1)]   # 12:00-16:00
        btc_future = [poisoned(c) for c in series("up", H1, end=AS_OF + H1.delta * 5, n=5, symbol=BTC)]
        w = world(extra=[fut15, forming_1h, forming_4h, btc_future])
        try:
            self.assertEqual(w.analyze().to_json(), ref)
        finally:
            w.close()

    def test_changing_the_last_closed_bar_changes_input_fingerprint(self):
        """Control: the look-ahead test is not vacuous."""
        a = world()
        b = world(seed=2)
        try:
            self.assertNotEqual(a.analyze().input_fingerprint, b.analyze().input_fingerprint)
        finally:
            a.close(), b.close()


class BtcContextTests(unittest.TestCase):
    def test_available(self):
        w = world(btc="down")
        try:
            ctx = w.analyze().btc_context
            self.assertEqual((ctx.status, ctx.regime, ctx.trend_direction),
                             (ContextStatus.AVAILABLE, MarketRegime.TRENDING_DOWN, TrendDirection.DOWN))
            self.assertEqual(ctx.data_through.isoformat(), "2026-03-02T12:00:00+00:00")
            self.assertEqual(ctx.timeframe, H1)
        finally:
            w.close()

    def test_unavailable_when_no_btc_data(self):
        w = world(btc=None)
        try:
            r = w.analyze()
            self.assertEqual(r.regime, MarketRegime.TRENDING_UP)       # symbol analysis still works
            ctx = r.btc_context
            self.assertEqual((ctx.status, ctx.regime), (ContextStatus.UNAVAILABLE, MarketRegime.UNKNOWN))
            self.assertIsNone(ctx.data_through)
            self.assertIn("BTC_CONTEXT_UNAVAILABLE", r.reason_codes)
        finally:
            w.close()

    def test_stale_btc(self):
        w = world(btc=None, extra=[series("up", H1, symbol=BTC, end=AS_OF - H1.delta * 3)])
        try:
            ctx = w.analyze().btc_context
            self.assertEqual((ctx.status, ctx.regime), (ContextStatus.STALE, MarketRegime.UNKNOWN))
        finally:
            w.close()

    def test_btc_itself_is_self_context(self):
        w = world({M15: "up", H1: "up", H4: "up"}, symbol=BTC, btc=None)
        try:
            ctx = w.analyze(symbol=BTC).btc_context
            self.assertEqual((ctx.status, ctx.regime), (ContextStatus.SELF, MarketRegime.TRENDING_UP))
        finally:
            w.close()


class RegimeDeterminismTests(unittest.TestCase):
    def test_same_input_same_snapshot_across_instances(self):
        a, b = world({M15: "range", H1: "up", H4: "down"}), world({M15: "range", H1: "up", H4: "down"})
        try:
            self.assertEqual(a.analyze().to_json(), b.analyze().to_json())
            self.assertEqual(a.analyze().to_json(), a.analyze().to_json())
        finally:
            a.close(), b.close()

    def test_versions_recorded(self):
        import app
        from app.research.regime.version import locked, rules_hash
        w = world()
        try:
            r = w.analyze()
            self.assertEqual(r.code_version, app.__version__)
            self.assertEqual(r.regime_version, locked()["regime_version"])
            self.assertEqual(r.config_hash, cfg().config_hash)
            self.assertTrue(r.feature_version.startswith("engine1:"))
        finally:
            w.close()
        self.assertEqual(rules_hash(), locked()["rules_hash"],
                         "regime rules changed: bump REGIME_VERSION if semantics changed, "
                         "then run scripts/lock_regime_version.py")


class RegimePersistenceTests(unittest.TestCase):
    def test_roundtrip_exact(self):
        w = world({M15: "turn_down", H1: "up", H4: "up"})
        try:
            r = w.analyze(save=True)
            back = w.store.load(r.key)
            self.assertEqual(back, r)
            self.assertEqual(back.to_json(), r.to_json())
            self.assertEqual(back.timeframes[0].metrics, r.timeframes[0].metrics)     # exact floats
        finally:
            w.close()

    def test_idempotent_and_restart(self):
        w = world()
        try:
            r = w.analyze(save=True)
            self.assertFalse(w.store.save(r))
            self.assertEqual(w.store.count(), 1)
            w.db.reopen()                                                  # "process restart"
            w.store = SQLiteRegimeSnapshotStore(w.db.writer)
            w.svc = RegimeService(FeatureService(w.db.store), cfg(), w.store)
            self.assertEqual(w.analyze(save=True), r)
            self.assertEqual(w.store.count(), 1)
        finally:
            w.close()

    def test_nondeterminism_detected_and_history_immutable(self):
        w = world()
        try:
            r = w.analyze(save=True)
            tampered = replace(r, reason_codes=r.reason_codes + ("EXTRA",))
            with self.assertRaises(StorageError):
                w.store.save(tampered)
            for sql in ("UPDATE regime_snapshots SET regime='ranging'", "DELETE FROM regime_snapshots"):
                with self.assertRaises(sqlite3.DatabaseError):
                    w.db.conn.execute(sql)
        finally:
            w.close()

    def test_new_thresholds_create_new_snapshot_not_overwrite(self):
        w = world()
        try:
            r1 = w.analyze(save=True)
            raw = json.loads(CONFIG_FILE.read_text())
            w.svc = RegimeService(FeatureService(w.db.store),
                                  RegimeConfig.from_mapping({**raw, "slope_min_atr": 0.6}), w.store)
            r2 = w.analyze(save=True)
            self.assertNotEqual(r1.config_hash, r2.config_hash)
            self.assertEqual(w.store.count(), 2)
            self.assertEqual(w.store.load(r1.key), r1)
        finally:
            w.close()

    def test_migration_chain(self):
        from app.storage.database import MIGRATIONS_DIR, connect, migrate
        d = Path(tempfile.mkdtemp())
        try:
            old = d / "m"
            old.mkdir()
            for f in sorted(MIGRATIONS_DIR.glob("*.sql"))[:3]:
                shutil.copy(f, old)
            c = connect(d / "x.sqlite3")
            migrate(c, old)
            self.assertEqual(migrate(c)[0], "0004_regime")      # later phases append after 0004
            c.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)


class RegimeCliTests(unittest.TestCase):
    def setUp(self):
        self.w = world({M15: "down", H1: "up", H4: "up"})
        self.env = {"DB_PATH": str(self.w.db.path), "EXECUTION_MODE": "disabled"}

    def tearDown(self):
        self.w.close()

    def _run(self, *argv):
        from app.cli.main import main
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, self.env):
            code = main(list(argv))
        return code, out.getvalue()

    def test_snapshot_structured_output(self):
        code, out = self._run("regime", "snapshot", "--symbol", "ETHUSDT", "--tf", "15m",
                              "--at", "2026-03-02T12:07:00+00:00", "--save")
        self.assertEqual(code, 0)
        d = json.loads(out)
        self.assertEqual(d["as_of"], "2026-03-02T12:00:00+00:00")        # floored to last closed bar
        self.assertEqual((d["regime"], d["mtf_alignment"]), ("trending_down", "conflict"))
        self.assertEqual(d["btc_context"]["status"], "available")
        self.assertIn("summary", d)
        self.assertEqual(self.w.store.count(), 1)
        code, out = self._run("regime", "history", "--symbol", "ETHUSDT", "--tf", "15m")
        self.assertIn("trending_down", out)

    def test_unknown_reasons_shown(self):
        code, out = self._run("regime", "snapshot", "--symbol", "SOLUSDT", "--at", "2026-03-02T12:00+00:00")
        d = json.loads(out)
        self.assertEqual(d["regime"], "unknown")
        self.assertTrue(d["unknown_reasons"])
        self.assertTrue(all("insufficient_history" in c or c.startswith("UNKNOWN:") for c in d["unknown_reasons"]))

    def test_refresh_via_synthetic_exchange(self):
        """--refresh path wired offline: backfill needed history from a fake Bybit, then classify."""
        from app.cli.regime_cmds import build, refresh
        from app.config.settings import load_settings
        from app.core.clock import FixedClock
        from app.domain.market import Symbol
        from tests.fakes import SyntheticBybit
        d = Path(tempfile.mkdtemp())
        try:
            s = load_settings(environ={"DB_PATH": str(d / "r.sqlite3")}, env_file=None)
            svc, md = build(s)
            class AnySymbolBybit(SyntheticBybit):          # synthetic exchange lists every symbol
                def get(self, url, params, timeout_s):
                    resp = super().get(url, params, timeout_s)
                    if url.endswith("/instruments-info") and "symbol" in params:
                        doc = json.loads(resp.body)
                        doc["result"]["list"][0]["symbol"] = params["symbol"]
                        doc["result"]["list"][0]["baseCoin"] = params["symbol"].removesuffix("USDT")
                        resp = replace(resp, body=json.dumps(doc).encode())
                    return resp
            syn = AnySymbolBybit(AS_OF + timedelta(minutes=5), AS_OF - timedelta(days=400))
            import app.cli.data_cmds as dc
            orig = dc.build

            def patched(settings, transport=None):
                bf, st, m = orig(settings, syn)
                bf.clock = bf.provider.clock = FixedClock(AS_OF + timedelta(minutes=5))
                bf.provider.client.executor.pacer.min_interval_s = 0
                return bf, st, m
            with mock.patch.object(dc, "build", patched):
                lines = refresh(s, svc, Symbol("ETHUSDT"), (M15, H1), AS_OF)
            self.assertEqual(len(lines), 3)                                  # 15m, 1h, BTC 1h
            r = svc.analyze(Symbol("ETHUSDT"), AS_OF, M15, (M15, H1))
            self.assertEqual(r.data_quality, QualityStatus.VALID)
            self.assertNotEqual(r.regime, MarketRegime.UNKNOWN)
            self.assertEqual(r.btc_context.status, ContextStatus.AVAILABLE)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class RegimeSafetyTests(unittest.TestCase):
    def test_no_llm_no_orders_in_regime_code(self):
        import ast
        root = Path(__file__).resolve().parents[1] / "app" / "research" / "regime"
        banned = {"anthropic", "openai", "gemini", "ollama", "submit", "place_order", "ExecutionGateway"}
        for f in root.rglob("*.py"):
            tree = ast.parse(f.read_text())
            names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
                    {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | \
                    {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
            self.assertEqual(names & banned, set(), f.name)

    def test_execution_ceiling_unchanged(self):
        from app.config.settings import BUILD_EXECUTION_CEILING, ExecutionMode
        self.assertEqual(BUILD_EXECUTION_CEILING, ExecutionMode.PAPER)
