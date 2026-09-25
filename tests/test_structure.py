"""Phase 5 market structure. Expected values are derived BY HAND from the paths in
tests/structure_data.py (comments below), not by re-running production logic."""
import ast
import json
import shutil
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from app.core.errors import StorageError
from app.domain.enums import (BreakMethod, LevelStatus, LiquiditySide, LiquiditySource, QualityStatus,
                              StructureDirection as D, StructureEventType as E, SwingKind)
from app.domain.errors import DomainError
from app.domain.market import Timeframe
from app.domain.structure import StructureSnapshot
from app.research.structure.config import StructureConfig
from app.research.structure.engine import detect_swings
from app.research.structure.service import StructureService
from tests.structure_data import (CONFIG_FILE, ETH, M15, PATH_A, PATH_B, ROOT, T0, WARMUP, bar_close, bar_open,
                                  candles, cfg, mirror, poisoned, random_walk, with_open_bar)


def compute(cs, as_of, **changes):
    return StructureService(None, cfg(**changes)).compute(cs, ETH, M15, as_of)


def idx(t):
    """path index of a bar open time"""
    return (t - T0) // M15.delta - WARMUP


def events(s):
    return [(e.event_type, e.direction, e.prior_direction, e.level_price, idx(e.break_bar_time)) for e in s.events]


A_END = bar_close(18)


class SwingDetectionTests(unittest.TestCase):
    def test_swings_of_path_a(self):
        # highs: 105@2, 108@10, 112@16 ; lows: 96@6, 101@13 ; confirmation = close of pivot + 2
        s = compute(candles(PATH_A), A_END)
        self.assertEqual([(idx(x.pivot_time), x.price, x.confirmed_at) for x in s.swing_highs],
                         [(2, 105.0, bar_close(4)), (10, 108.0, bar_close(12)), (16, 112.0, bar_close(18))])
        self.assertEqual([(idx(x.pivot_time), x.price, x.confirmed_at) for x in s.swing_lows],
                         [(6, 96.0, bar_close(8)), (13, 101.0, bar_close(15))])
        self.assertTrue(all(x.algorithm == "fractal-v1" and x.symbol == "ETHUSDT" and x.timeframe is M15
                            for x in s.swing_highs + s.swing_lows))

    def test_equal_extremes_tie_rule_earliest_wins(self):
        h = [1, 2, 5, 5, 3, 2, 1]
        l = [0] * 7
        self.assertEqual(detect_swings(h, l, 2, 2)[0], [2])      # bar 3 fails strict-left vs bar 2
        h = [1, 2, 5, 4, 5, 2, 1]                                  # 5@2 and 5@4 are only 2 bars apart
        self.assertEqual(detect_swings(h, l, 2, 2)[0], [2])

    def test_consecutive_close_extremes(self):
        h = [1, 2, 5, 3, 6, 3, 2, 1]                               # 5@2 is dominated by 6@4 within right bars
        self.assertEqual(detect_swings(h, [0] * 8, 2, 2)[0], [4])

    def test_not_known_before_confirmation(self):
        cs = candles(PATH_A)
        self.assertNotIn(2, [idx(x.pivot_time) for x in compute(cs, bar_close(3)).swing_highs])
        known = compute(cs, bar_close(4)).swing_highs
        self.assertEqual([(idx(x.pivot_time), x.confirmed_at) for x in known], [(2, bar_close(4))])

    def test_configurable_confirmation_bars(self):
        s = compute(candles(PATH_A), A_END, swing_right=3)
        self.assertEqual([x.confirmed_at for x in s.swing_highs][:1], [bar_close(5)])


class BosTests(unittest.TestCase):
    def test_bos_and_first_break(self):
        s = compute(candles(PATH_A), A_END)
        ev = events(s)
        self.assertEqual(ev[0], (E.BREAK_UNCLASSIFIED, D.BULLISH, D.UNKNOWN, 105.0, 9))   # close 106 > 105
        self.assertEqual(ev[1], (E.BOS, D.BULLISH, D.BULLISH, 108.0, 14))                  # close 109 > 108
        bos = s.last_bos
        self.assertEqual((bos.swing_pivot_time, bos.swing_confirmed_at, bos.event_time, bos.break_price),
                         (bar_open(10), bar_close(12), bar_close(14), 109.0))
        self.assertEqual((bos.method, bos.data_quality), (BreakMethod.CLOSE, QualityStatus.VALID))
        self.assertIn("CONTINUATION", bos.reason_codes)

    def test_no_repeated_bos_on_following_bars(self):
        # bars 15, 16 also close above 108: the level is already broken -> no new events
        s = compute(candles(PATH_A), bar_close(16))
        self.assertEqual([x[4] for x in events(s)], [9, 14])

    def test_wick_without_close_is_not_a_break(self):
        # PATH_B bar 9: high 106.5 above swing high 105.9, close 105 below it
        self.assertEqual(compute(candles(PATH_B), bar_close(10)).events, ())

    def test_wick_method_is_explicit_and_separate(self):
        s = compute(candles(PATH_B), bar_close(10), break_confirmation="wick")
        self.assertEqual(events(s), [(E.BREAK_UNCLASSIFIED, D.BULLISH, D.UNKNOWN, 105.9, 9)])
        self.assertEqual((s.events[0].method, s.events[0].break_price), (BreakMethod.WICK, 106.5))

    def test_domain_rejects_break_of_unconfirmed_level(self):
        e = compute(candles(PATH_A), A_END).last_bos
        with self.assertRaises(DomainError):
            replace(e, swing_confirmed_at=e.break_bar_time + M15.delta)


class ChochTests(unittest.TestCase):
    def test_bearish_choch_after_bullish_structure(self):
        s = compute(candles(PATH_A), A_END)
        self.assertEqual(events(s)[2], (E.CHOCH, D.BEARISH, D.BULLISH, 101.0, 18))   # close 100 < 101
        self.assertEqual((s.direction, s.direction_since), (D.BEARISH, bar_close(18)))
        self.assertIn("CHANGE_OF_CHARACTER", s.last_choch.reason_codes)

    def test_mirror_same_bars_opposite_labels(self):
        """Same geometry, opposite direction: a bearish break is BOS after bearish structure and a
        bullish break is CHoCH -> the label depends on the prior direction, not on the break alone."""
        s = compute(candles(mirror(PATH_A)), A_END)
        self.assertEqual(events(s), [(E.BREAK_UNCLASSIFIED, D.BEARISH, D.UNKNOWN, 95.0, 9),
                                     (E.BOS, D.BEARISH, D.BEARISH, 92.0, 14),
                                     (E.CHOCH, D.BULLISH, D.BEARISH, 99.0, 18)])

    def test_unknown_direction_is_not_guessed(self):
        s = compute(candles(PATH_A), bar_close(8))            # no break yet
        self.assertEqual((s.direction, s.events, s.last_bos, s.last_choch), (D.UNKNOWN, (), None, None))
        self.assertIn("DIRECTION_UNKNOWN:no_structure_break_in_window", s.reason_codes)

    def test_domain_invariants_of_labels(self):
        e = compute(candles(PATH_A), A_END).last_choch
        for bad in (dict(event_type=E.BOS), dict(prior_direction=D.UNKNOWN),
                    dict(event_type=E.BREAK_UNCLASSIFIED)):
            with self.subTest(bad=bad), self.assertRaises(DomainError):
                replace(e, **bad)


class EqualLevelTests(unittest.TestCase):
    def test_equal_highs_formed_with_atr_tolerance(self):
        s = compute(candles(PATH_B), bar_close(8))               # before the sweep
        (eqh,) = s.equal_highs
        self.assertEqual([idx(t) for t in eqh.source_pivots], [2, 6])
        self.assertEqual((eqh.price, eqh.zone_low, eqh.zone_high), (106.0, 105.9, 106.0))
        self.assertEqual((eqh.created_at, eqh.side, eqh.status), (bar_close(8), LiquiditySide.BUY_SIDE,
                                                                  LevelStatus.ACTIVE))
        self.assertAlmostEqual(eqh.tolerance, 0.1 * eqh.atr_at_creation)
        self.assertTrue(1.5 < eqh.atr_at_creation < 4)          # ATR of the calm warmup (~2) plus the path

    def test_equal_lows_exact(self):
        (eql,) = compute(candles(PATH_B), bar_close(10)).equal_lows
        self.assertEqual(([idx(t) for t in eql.source_pivots], eql.price), ([4, 8], 100.0))

    def test_outside_tolerance_not_merged(self):
        path = list(PATH_B)
        path[6] = (105.0, 102, 104)                              # 1.0 below 106 >> 0.1 ATR
        self.assertEqual(compute(candles(path), bar_close(8)).equal_highs, ())

    def test_separation_limit(self):
        self.assertEqual(compute(candles(PATH_B), bar_close(8), eq_max_separation_bars=3).equal_highs, ())

    def test_atr_unavailable_no_fake_tolerance(self):
        s = compute(candles(PATH_B, warmup=20), bar_close(10, warmup=20), min_bars=20, window_bars=40)
        self.assertEqual((s.equal_highs, s.equal_lows), ((), ()))
        self.assertTrue(any(c.startswith("EQ_TOLERANCE_UNAVAILABLE") for c in s.reason_codes))
        self.assertTrue(s.liquidity)                             # swing levels still exist


class LiquidityTests(unittest.TestCase):
    def test_active_levels_path_a(self):
        # 105 and 108 were CLOSED through (invalidated), 101 closed through at bar 18; 112 and 96 untouched
        s = compute(candles(PATH_A), A_END)
        self.assertEqual(sorted((lv.side, lv.source, lv.price) for lv in s.liquidity),
                         [(LiquiditySide.BUY_SIDE, LiquiditySource.SWING_HIGH, 112.0),
                          (LiquiditySide.SELL_SIDE, LiquiditySource.SWING_LOW, 96.0)])

    def test_level_known_only_after_confirmation_and_invalidated_by_close(self):
        cs = candles(PATH_A)
        at8 = {lv.price for lv in compute(cs, bar_close(8)).liquidity}
        at9 = {lv.price for lv in compute(cs, bar_close(9)).liquidity}
        self.assertIn(105.0, at8)
        self.assertNotIn(105.0, at9)                             # bar 9 closed above -> INVALIDATED, not swept
        self.assertEqual(compute(cs, bar_close(9)).sweeps, ())

    def test_merged_members_not_double_counted(self):
        s = compute(candles(PATH_B), bar_close(8))
        buy = [lv for lv in s.liquidity if lv.side is LiquiditySide.BUY_SIDE]
        self.assertEqual([lv.source for lv in buy], [LiquiditySource.EQUAL_HIGHS])

    def test_expiry(self):
        s = compute(candles(PATH_A), A_END, level_expiry_bars=5)
        self.assertNotIn(96.0, [lv.price for lv in s.liquidity])  # created close 8, untouched, 10 bars old
        self.assertIn(112.0, [lv.price for lv in s.liquidity])    # created at close 18


class SweepTests(unittest.TestCase):
    def test_sweep_of_equal_highs(self):
        s = compute(candles(PATH_B), bar_close(10))
        (sw,) = s.sweeps
        self.assertEqual((sw.side, sw.level_price, idx(sw.bar_time), sw.event_time, sw.extreme, sw.close),
                         (LiquiditySide.BUY_SIDE, 106.0, 9, bar_close(9), 106.5, 105.0))
        self.assertEqual(sw.method, "wick_through_close_back_same_bar")
        self.assertEqual(s.equal_highs, ())

    def test_touch_is_not_a_sweep(self):
        # lows touch exactly 100 on bars 5 and 8 -> the 100 level stays ACTIVE
        s = compute(candles(PATH_B), bar_close(10))
        self.assertIn(100.0, [lv.price for lv in s.liquidity if lv.side is LiquiditySide.SELL_SIDE])
        self.assertFalse([w for w in s.sweeps if w.side is LiquiditySide.SELL_SIDE])

    def test_sweep_not_visible_before_its_bar_closes(self):
        self.assertEqual(compute(candles(PATH_B), bar_close(8)).sweeps, ())


class NoLookAheadTests(unittest.TestCase):
    def test_future_bars_never_change_past_snapshots(self):
        for path, last in ((PATH_A, 18), (PATH_B, 10), (mirror(PATH_A), 18)):
            full = candles(path)
            for t in range(0, last + 1):
                cut = [c for c in full if c.close_time <= bar_close(t)]
                future = [poisoned(c) for c in full if c.close_time > bar_close(t)]
                with self.subTest(t=t):
                    self.assertEqual(compute(cut + future, bar_close(t)).to_json(),
                                     compute(cut, bar_close(t)).to_json())

    def test_snapshot_rejects_future_information(self):
        s = compute(candles(PATH_A), A_END)
        with self.assertRaises(DomainError):
            replace(s, as_of=bar_close(13))                    # contains facts confirmed later


class ClosedCandleTests(unittest.TestCase):
    def test_open_candle_is_ignored(self):
        cs = candles(PATH_A)
        ref = compute(cs, A_END).to_json()
        self.assertEqual(compute(with_open_bar(cs, 200, 50, 60), A_END).to_json(), ref)

    def test_as_of_inside_forming_bar_is_stale(self):
        s = compute(with_open_bar(candles(PATH_A), 200, 50, 60), A_END + M15.delta)
        self.assertEqual((s.data_quality, s.events, s.liquidity), (QualityStatus.STALE, (), ()))


class GapTests(unittest.TestCase):
    def test_gap_inside_required_window_is_failure(self):
        s = compute(candles(PATH_A, missing={8}), A_END)
        self.assertEqual((s.data_quality, s.direction, s.events), (QualityStatus.GAP, D.UNKNOWN, ()))

    def test_structure_restarts_after_gap(self):
        # gap at path bar 8, then enough bars: structure never bridges the gap
        cs = candles(PATH_A, missing={8}) + candles([(101, 99, 100)] * 120, warmup=0,
                                                    start=bar_open(19))
        s = compute(cs, bar_close(18 + 120))
        self.assertIn("HISTORY_BEFORE_LAST_GAP_IGNORED", s.reason_codes)
        self.assertGreater(s.window_start, bar_open(8))
        self.assertTrue(all(e.swing_pivot_time > bar_open(8) for e in s.events))
        self.assertTrue(all(sw.pivot_time > bar_open(8) for sw in s.swing_highs + s.swing_lows))


class InsufficientAndInvalidDataTests(unittest.TestCase):
    def test_insufficient_history(self):
        s = compute(candles(PATH_A, warmup=10), bar_close(18, warmup=10))
        self.assertEqual((s.data_quality, s.liquidity), (QualityStatus.INSUFFICIENT_HISTORY, ()))
        self.assertTrue(any(c.startswith("STRUCTURE_UNAVAILABLE:15m:insufficient_history") for c in s.reason_codes))

    def test_no_data(self):
        self.assertEqual(compute([], A_END).data_quality, QualityStatus.MISSING_HISTORY)

    def test_duplicates_make_series_unusable(self):
        cs = candles(PATH_A)
        self.assertEqual(compute(cs + [cs[-5]], A_END).data_quality, QualityStatus.DUPLICATE)

    def test_domain_refuses_structure_on_bad_data(self):
        s = compute(candles(PATH_A), A_END)
        with self.assertRaises(DomainError):
            replace(s, data_quality=QualityStatus.GAP)


class DeterminismTests(unittest.TestCase):
    def test_same_input_same_output(self):
        cs = random_walk(1500, 3)
        a = compute(cs, T0 + M15.delta * 1500).to_json()
        b = compute(list(reversed(cs)), T0 + M15.delta * 1500).to_json()
        self.assertEqual(a, b)
        self.assertEqual(StructureSnapshot.from_json(a).to_json(), a)

    def test_stable_ids(self):
        s1, s2 = compute(candles(PATH_A), A_END), compute(candles(PATH_A), A_END)
        self.assertEqual([e.event_id for e in s1.events], [e.event_id for e in s2.events])
        self.assertEqual(len({e.event_id for e in s1.events}), 3)

    def test_version_lock(self):
        from app.research.structure.version import locked, rules_hash
        self.assertEqual(rules_hash(), locked()["rules_hash"],
                         "structure rules changed: bump STRUCTURE_VERSION if semantics changed, "
                         "then run scripts/lock_structure_version.py")


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        from tests.fakes import TempDB
        from app.storage.structure import SQLiteStructureStore
        self.db = TempDB()
        self.store = SQLiteStructureStore(self.db.writer)
        self.svc = StructureService(self.db.store, cfg(), self.store)

    def tearDown(self):
        self.db.close()

    def test_roundtrip_and_idempotency(self):
        s = compute(candles(PATH_A), A_END)
        self.assertTrue(self.store.save(s))
        self.assertEqual(self.store.last.events_inserted, 3)
        self.assertFalse(self.store.save(s))
        self.assertEqual((self.store.last.events_inserted, self.store.last.events_known), (0, 3))
        self.assertEqual(self.store.load(s.key), s)
        self.assertEqual((self.store.count(), self.store.count("structure_events")), (1, 3))

    def test_bar_by_bar_replay_records_each_event_once(self):
        self.db.store.upsert_candles(candles(PATH_A), "t")
        for t in range(0, 19):
            self.svc.analyze(ETH, M15, bar_close(t), save=True)
        self.assertEqual(self.store.count(), 19)
        kinds = [e.event_type.value if hasattr(e, "event_type") else "sweep" for e in self.store.events(ETH, M15)]
        self.assertEqual(sorted(kinds), ["bos", "break_unclassified", "choch"])

    def test_reclassification_keeps_first_record(self):
        s = compute(candles(PATH_A), A_END)
        self.store.save(s)
        bos = s.last_bos
        relabeled = replace(bos, event_type=E.BREAK_UNCLASSIFIED, prior_direction=D.UNKNOWN)
        later = replace(s, as_of=s.as_of + M15.delta, input_fingerprint="other", events=(relabeled,),
                        last_bos=None, last_choch=None, swing_highs=(), swing_lows=())
        self.store.save(later)
        self.assertEqual(self.store.last.conflicts, 1)
        (stored,) = [e for e in self.store.events(ETH, M15) if e.event_id == bos.event_id]
        self.assertEqual(stored.event_type, E.BOS)
        self.assertEqual(self.store.count("structure_event_conflicts"), 1)

    def test_nondeterminism_detected_and_history_immutable(self):
        s = compute(candles(PATH_A), A_END)
        self.store.save(s)
        with self.assertRaises(StorageError):
            self.store.save(replace(s, reason_codes=s.reason_codes + ("X",)))
        for sql in ("UPDATE structure_snapshots SET direction='bullish'", "DELETE FROM structure_snapshots",
                    "UPDATE structure_events SET kind='bos'", "DELETE FROM structure_events"):
            with self.assertRaises(sqlite3.DatabaseError):
                self.db.conn.execute(sql)

    def test_migration_chain(self):
        from app.storage.database import MIGRATIONS_DIR, connect, migrate
        d = Path(tempfile.mkdtemp())
        try:
            old = d / "m"
            old.mkdir()
            for f in sorted(MIGRATIONS_DIR.glob("*.sql"))[:5]:
                shutil.copy(f, old)
            c = connect(d / "x.sqlite3")
            migrate(c, old)
            self.assertEqual(migrate(c)[0], "0006_structure")
            c.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)


class MtfAlignmentTests(unittest.TestCase):
    def test_each_timeframe_on_its_own_closed_bars(self):
        from tests.fakes import TempDB
        from tests.regime_data import series
        db = TempDB()
        try:
            as_of = T0 + timedelta(days=60, hours=13, minutes=30)
            H1, H4 = Timeframe.H1, Timeframe.H4
            for tf in (M15, H1, H4):
                db.store.upsert_candles(series("up", tf, end=tf.floor(as_of), symbol=ETH), "t")
            # bars closing AFTER as_of are in the store (e.g. written later) and must stay invisible
            for tf in (H1, H4):
                db.store.upsert_candles([poisoned(c) for c in series("up", tf, end=tf.floor(as_of) + tf.delta * 3,
                                                                       n=3, symbol=ETH)], "t")
            svc = StructureService(db.store, cfg())
            got = {tf: svc.analyze(ETH, tf, as_of) for tf in (M15, H1, H4)}
            self.assertEqual({tf.value: s.as_of.strftime("%H:%M") for tf, s in got.items()},
                             {"15m": "13:30", "1h": "13:00", "4h": "12:00"})
            for tf, s in got.items():
                self.assertEqual(s.data_quality, QualityStatus.VALID)
                self.assertTrue(all(x.timeframe is tf for x in s.swing_highs + s.swing_lows))
                self.assertTrue(all(e.timeframe is tf for e in s.events))
                self.assertTrue(all(lv.price < 999999 for lv in s.liquidity))
        finally:
            db.close()


class StructureConfigTests(unittest.TestCase):
    def test_no_hidden_defaults(self):
        raw = json.loads(CONFIG_FILE.read_text())
        for k in [k for k in raw if not k.startswith("_") and k != "schema"]:
            with self.subTest(missing=k), self.assertRaises(DomainError):
                StructureConfig.from_mapping({x: y for x, y in raw.items() if x != k})

    def test_invalid_values(self):
        raw = json.loads(CONFIG_FILE.read_text())
        for bad in ({"swing_left": 0}, {"break_confirmation": "body"}, {"eq_tolerance_atr": 0},
                    {"atr_feature": "cvd"}, {"atr_feature": "ret_std_20"}, {"atr_feature": "nope"},
                    {"window_bars": 50, "min_bars": 100}, {"min_bars": 4}, {"eq_max_separation_bars": 1},
                    {"level_expiry_bars": True}):
            with self.subTest(bad=bad), self.assertRaises(DomainError):
                StructureConfig.from_mapping({**raw, **bad})

    def test_hash_tracks_parameters(self):
        self.assertNotEqual(cfg().config_hash, cfg(swing_left=3).config_hash)

    def test_no_numeric_thresholds_in_engine(self):
        """Every threshold comes from StructureConfig. Allowed literals: 0/1, tuple
        indexing (x[2]) and the id length."""
        tree = ast.parse((ROOT / "app" / "research" / "structure" / "engine.py").read_text())
        index_consts = {id(n.slice) for n in ast.walk(tree) if isinstance(n, ast.Subscript)}
        nums = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and id(n) not in index_consts
                and isinstance(n.value, (int, float)) and not isinstance(n.value, bool) and n.value not in (0, 1)]
        self.assertEqual(nums, [24], "only the id length literal is allowed")


class StructureSafetyTests(unittest.TestCase):
    def test_no_orders_no_llm_no_network(self):
        root = ROOT / "app" / "research" / "structure"
        banned = {"submit", "place_order", "ExecutionGateway", "anthropic", "openai", "gemini", "ollama",
                  "urlopen", "requests", "BybitRestClient", "UrllibTransport"}
        for f in root.rglob("*.py"):
            tree = ast.parse(f.read_text())
            ids = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
                  {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | \
                  {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
            self.assertEqual(ids & banned, set(), f.name)

    def test_no_order_book_claims(self):
        src = (ROOT / "app" / "domain" / "structure.py").read_text()
        self.assertIn("not observations of\nthe order book", src)
        from app.domain.structure import StructureSnapshot as S
        self.assertFalse({"orderbook", "order_book", "confidence", "wall"} & set(S.__dataclass_fields__))
