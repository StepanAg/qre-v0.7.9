"""Phase 6 setup detection. Expected lifecycles are derived BY HAND from the
fixture paths (bar indices are path indices; see tests/setup_data.py):

PATH_A  swings H@2=105 H@10=108 H@16=112, L@6=96 L@13=101
        bar 9  close 106 > 105  -> BREAK_UNCLASSIFIED (no setup: direction unknown before)
        bar 14 close 109 > 108  -> BOS bullish  => breakout candidate @14
        bars 15,16 close 110,111 >= 108 -> 2 holds => breakout confirmed @16
        bar 17 close 103 < 108 -> breakout invalidated @17
        pullback: zone = 0.5*ATR(14) (ATR 2..4 -> zone < 2): bar 15 close 110 > 108+zone = departed,
        bar 16 low 107 <= 108+zone, close 111 >= 108-zone -> candidate @16; bar 17 close 103 -> invalidated @17
        bar 18 close 100 < 101 -> CHoCH bearish (no breakout: CHoCH is not a continuation trigger)
PATH_B  EQH 106/105.9; bar 9 wick 106.5 close 105 (sweep of buy-side) low 100.5; SL@4=100 active
PATH_S1 bar 11 close 99.5 < sweep-bar low 100.5 -> sweep reversal confirmed @11
PATH_S2 bar 11 close 107 > sweep extreme 106.5 -> invalidated @11
PATH_R  top 110 (H@2), bottom 100 (L@6); bar 10 high 109.8 (>= 110 - 0.3 ATR, <= 110) close 106 in the
        lower half -> bearish range rejection candidate @10; bar 11 close 105 < 105.5 -> confirmed @11
"""
import ast
import json
import unittest
from dataclasses import replace
from datetime import timedelta

from app.domain.enums import QualityStatus, RegimeFit, SetupStatus, StructureDirection
from app.domain.errors import DomainError
from app.research.setup.config import SetupConfig
from tests.setup_data import (EVAL, PATH_A, PATH_B, PATH_N, PATH_P, PATH_R, PATH_S1, PATH_S2, SETUP_CONFIG, W,
                              by_type, evaluate, life, service)
from tests.structure_data import ETH, M15, ROOT, T0, bar_close, candles, poisoned

BULL, BEAR = StructureDirection.BULLISH, StructureDirection.BEARISH


def regime(kind):
    """A real RegimeSnapshot from the Phase 3 engine on hand-set features."""
    from app.research.regime.engine import RegimeEngine
    from tests.regime_data import atr_units, cfg, snap
    feats = {"up": atr_units(spread=2, slope=1, disp=1, ret=2), "down": atr_units(spread=-2, slope=-1, disp=-1, ret=-2),
             "range": atr_units(spread=0.3, slope=0.1, disp=0.2)}[kind]
    return RegimeEngine(cfg()).classify(ETH, snap(feats), M15, (M15,))


class BreakoutTests(unittest.TestCase):
    def test_lifecycle_confirm_then_fail(self):
        (b,) = by_type(evaluate(PATH_A, 18), "breakout")
        self.assertEqual(life(b), [("candidate", 14, "TRIGGER:BREAKOUT"), ("confirmed", 16, "HELD_BEYOND_LEVEL_2_CLOSES"),
                                   ("invalidated", 17, "CLOSE_BACK_INSIDE_BROKEN_LEVEL")])
        self.assertEqual((b.direction, b.key_level, b.invalidation_condition), (BULL, 108.0, "close < 108.0"))
        self.assertEqual((idx_close(b.trigger_time), b.confirmed_at, b.closed_at), (14, bar_close(16, warmup=W),
                                                                                    bar_close(17, warmup=W)))

    def test_only_bos_triggers(self):
        s = evaluate(PATH_A, 18)
        self.assertEqual([x.key_level for x in by_type(s, "breakout")], [108.0])   # not 105 (unclassified), not 101

    def test_expiry(self):
        (b,) = by_type(evaluate(PATH_A, 18, ttl_bars=1), "breakout")
        self.assertEqual(life(b), [("candidate", 14, "TRIGGER:BREAKOUT"), ("expired", 15, "TTL_ELAPSED")])

    def test_close_exactly_at_level_is_a_hold(self):
        path = PATH_A[:15] + [(110, 107, 108), (111, 107.5, 110)]
        (b,) = by_type(evaluate(path, 16), "breakout")
        self.assertEqual([x[0] for x in life(b)], ["candidate", "confirmed"])

    def test_not_known_before_the_bos(self):
        self.assertEqual(by_type(evaluate(PATH_A, 13), "breakout"), [])


class PullbackTests(unittest.TestCase):
    def test_return_then_level_lost(self):
        s = evaluate(PATH_A, 18)
        (p,) = by_type(s, "pullback")
        atr = dict(p.evidence)["atr"]
        self.assertTrue(2 < atr < 4, "fixture assumption (zone < 2) holds")
        self.assertEqual(life(p), [("candidate", 16, "TRIGGER:PULLBACK"), ("invalidated", 17, "LEVEL_LOST")])

    def test_confirmed_by_resumption(self):
        (p,) = by_type(evaluate(PATH_P, 17), "pullback")
        self.assertEqual(life(p), [("candidate", 16, "TRIGGER:PULLBACK"), ("confirmed", 17, "CLOSE_BEYOND_TOUCH_BAR")])

    def test_approach_alone_is_only_a_candidate(self):
        (p,) = by_type(evaluate(PATH_P, 16), "pullback")
        self.assertEqual(p.status, SetupStatus.CANDIDATE)

    def test_no_return_no_pullback(self):
        self.assertEqual(by_type(evaluate(PATH_N, 17), "pullback"), [])

    def test_bar_after_bos_is_not_an_automatic_touch(self):
        # PATH_A bar 15 dips to 105 but departure only completes at ITS close -> no candidate on bar 15
        (p,) = by_type(evaluate(PATH_A, 18), "pullback")
        self.assertEqual(life(p)[0][1], 16)


class SweepReversalTests(unittest.TestCase):
    def test_confirmed_by_close_beyond_sweep_bar(self):
        (w,) = by_type(evaluate(PATH_S1, 11), "sweep_reversal")
        self.assertEqual((w.direction, w.key_level), (BEAR, 106.0))
        self.assertEqual(life(w), [("candidate", 9, "TRIGGER:SWEEP_REVERSAL"), ("confirmed", 11, "CLOSE_BEYOND_SWEEP_BAR")])
        self.assertEqual(dict(w.evidence)["sweep_extreme"], 106.5)

    def test_invalidated_when_price_accepts_above(self):
        (w,) = by_type(evaluate(PATH_S2, 11), "sweep_reversal")
        self.assertEqual(life(w)[-1], ("invalidated", 11, "CLOSE_BEYOND_SWEEP_EXTREME"))

    def test_sweep_is_not_automatically_a_reversal(self):
        for last in (9, 10):
            (w,) = by_type(evaluate(PATH_B, last), "sweep_reversal")
            self.assertEqual(w.status, SetupStatus.CANDIDATE)

    def test_structure_break_confirmation_method(self):
        # bar 11 close 99.5 < swing low 100 (L@8, confirmed at bar 10) -> bearish structure break at 11
        (w,) = by_type(evaluate(PATH_S1, 11, sweep_confirmation="opposite_structure_break"), "sweep_reversal")
        self.assertEqual(life(w)[-1], ("confirmed", 11, "OPPOSITE_STRUCTURE_BREAK"))


class RangeRejectionTests(unittest.TestCase):
    def test_top_rejection(self):
        (r,) = by_type(evaluate(PATH_R, 11), "range_rejection")
        self.assertEqual((r.direction, r.key_level), (BEAR, 110.0))
        self.assertEqual(life(r), [("candidate", 10, "TRIGGER:RANGE_REJECTION"),
                                   ("confirmed", 11, "CLOSE_BEYOND_REJECTION_BAR")])
        ev = dict(r.evidence)
        self.assertEqual((ev["range_top"], ev["range_bottom"]), (110.0, 100.0))

    def test_piercing_is_not_a_rejection(self):
        path = PATH_R[:10] + [(110.5, 105.5, 106), (107, 104.5, 105)]
        self.assertEqual(by_type(evaluate(path, 11), "range_rejection"), [])

    def test_close_in_upper_part_is_not_a_rejection(self):
        path = PATH_R[:10] + [(109.8, 105.5, 109.5)]            # touches the top but closes near its high
        self.assertEqual([s for s in by_type(evaluate(path, 10), "range_rejection") if s.direction is BEAR], [])

    def test_repeated_rejection_of_same_boundary_is_one_setup(self):
        path = PATH_R[:11] + [(109.9, 105.2, 105.5)]           # bar 11 rejects 110 again, first still open
        self.assertEqual(len([s for s in by_type(evaluate(path, 11), "range_rejection") if s.direction is BEAR]), 1)


class DirectionDataAndRegimeTests(unittest.TestCase):
    def test_unknown_direction_creates_nothing(self):
        s = evaluate(PATH_A, 12)                     # only the unclassified break at bar 9 exists
        self.assertEqual(s.setups, ())
        self.assertEqual(s.data_quality, QualityStatus.VALID)

    def test_insufficient_history(self):
        s = evaluate(PATH_A, 18, warmup=110)
        self.assertEqual((s.data_quality, s.setups), (QualityStatus.INSUFFICIENT_HISTORY, ()))
        self.assertTrue(any(c.startswith("SETUP_UNAVAILABLE:15m:insufficient_history") for c in s.reason_codes))

    def test_gap_inside_replay_window(self):
        cs = candles(PATH_A, warmup=W, missing={12})
        s = service().compute(cs, ETH, M15, bar_close(18, warmup=W), None, EVAL)
        self.assertEqual((s.data_quality, s.setups), (QualityStatus.GAP, ()))

    def test_timeframe_not_enabled(self):
        s = evaluate(PATH_A, 18, timeframes=["1h"])
        self.assertEqual(s.setups, ())
        self.assertIn("TIMEFRAME_NOT_ENABLED:15m", s.reason_codes)

    def test_disabled_type(self):
        s = evaluate(PATH_A, 18, enabled_types=["breakout"])
        self.assertEqual([x.setup_type.value for x in s.setups], ["breakout"])

    def test_regime_does_not_change_lifecycle_only_fit(self):
        base = evaluate(PATH_R, 11)
        with_range, with_up = evaluate(PATH_R, 11, regime=regime("range")), evaluate(PATH_R, 11, regime=regime("up"))
        for s in (base, with_range, with_up):
            self.assertEqual([life(x) for x in s.setups], [life(x) for x in base.setups])
        self.assertEqual(base.setups[0].regime_fit, RegimeFit.UNKNOWN)
        self.assertEqual(with_range.setups[0].regime_fit, RegimeFit.ALLOWED)
        r = with_up.setups[0]
        self.assertEqual(r.regime_fit, RegimeFit.NOT_ALLOWED)
        self.assertIn("REGIME_NOT_ALLOWED:trending_up", r.contradictions)
        self.assertIn("REGIME_CONTEXT_UNAVAILABLE", base.reason_codes)
        self.assertEqual(with_up.regime_context["regime"], "trending_up")

    def test_regime_trend_opposing_a_continuation_is_a_contradiction(self):
        (b,) = by_type(evaluate(PATH_P, 17, regime=regime("down")), "breakout")
        self.assertEqual(b.regime_fit, RegimeFit.ALLOWED)            # trending_down is an allowed breakout regime
        self.assertIn("REGIME_TREND_OPPOSES:down", b.contradictions)

    def test_conflicting_setups_on_one_bar(self):
        # PATH_B bar 9: sweep of buy-side (bearish candidate) AND rejection of range bottom 100 (bullish)
        s = evaluate(PATH_B, 9)
        dirs = sorted((x.setup_type.value, x.direction.value) for x in s.setups)
        self.assertEqual(dirs, [("range_rejection", "bullish"), ("sweep_reversal", "bearish")])
        self.assertIn("CONFLICTING_ACTIVE_SETUPS", s.reason_codes)
        self.assertTrue(all(any(c.startswith("OPPOSITE_SETUP_ACTIVE") for c in x.contradictions) for x in s.setups))

    def test_domain_separates_notions(self):
        (b,) = by_type(evaluate(PATH_A, 18), "breakout")
        for bad in (dict(direction=StructureDirection.UNKNOWN), dict(data_quality=QualityStatus.GAP),
                    dict(transitions=b.transitions[1:])):
            with self.subTest(bad=bad), self.assertRaises(DomainError):
                replace(b, **bad)
        self.assertFalse({"score", "confidence", "tradeable", "signal"} & set(type(b).__dataclass_fields__))


class PointInTimeTests(unittest.TestCase):
    def test_future_bars_never_change_past_evaluations(self):
        for path, last in ((PATH_A, 18), (PATH_P, 17), (PATH_S1, 11), (PATH_R, 11)):
            full = candles(path, warmup=W)
            for t in range(max(0, last - 8), last + 1):
                cut = [c for c in full if c.close_time <= bar_close(t, warmup=W)]
                future = [poisoned(c) for c in full if c.close_time > bar_close(t, warmup=W)]
                with self.subTest(t=t):
                    a = service().compute(cut + future, ETH, M15, bar_close(t, warmup=W), None, EVAL)
                    b = service().compute(cut, ETH, M15, bar_close(t, warmup=W), None, EVAL)
                    self.assertEqual(a.canonical_json(), b.canonical_json())

    def test_history_is_append_only_across_as_of(self):
        """A setup seen at t appears at t+1 with the same identity and its transitions extended, never rewritten."""
        prev = None
        for t in range(12, 19):
            cur = {s.setup_id: s for s in evaluate(PATH_A, t).setups}
            if prev:
                for sid, s in prev.items():
                    self.assertIn(sid, cur)
                    self.assertEqual(cur[sid].identity(), s.identity())
                    self.assertEqual(cur[sid].transitions[:len(s.transitions)], s.transitions)
            prev = cur

    def test_snapshot_rejects_future_facts(self):
        s = evaluate(PATH_A, 18)
        with self.assertRaises(DomainError):
            replace(s, as_of=bar_close(15, warmup=W))

    def test_longer_history_same_setups(self):
        a, b = evaluate(PATH_A, 18, warmup=W), evaluate(PATH_A, 18, warmup=W + 150)
        strip = lambda s: [(x.setup_type, x.direction, x.key_level, [(t.status, t.reason) for t in x.transitions])
                           for x in s.setups]  # noqa: E731
        self.assertEqual(strip(a), strip(b))


class DeterminismTests(unittest.TestCase):
    def test_same_input_same_output(self):
        a = evaluate(PATH_S1, 11, regime=regime("range"))
        b = evaluate(PATH_S1, 11, regime=regime("range"))
        self.assertEqual(a.canonical_json(), b.canonical_json())
        from app.domain.setup import SetupSnapshot
        self.assertEqual(SetupSnapshot.from_dict(json.loads(a.canonical_json()), EVAL), a)

    def test_evaluated_at_is_not_part_of_the_result(self):
        from datetime import datetime, timezone
        cs = candles(PATH_A, warmup=W)
        a = service().compute(cs, ETH, M15, bar_close(18, warmup=W), None, EVAL)
        b = service().compute(cs, ETH, M15, bar_close(18, warmup=W), None, datetime.now(timezone.utc))
        self.assertEqual(a.canonical_json(), b.canonical_json())

    def test_version_lock(self):
        from app.research.setup.version import locked, rules_hash
        self.assertEqual(rules_hash(), locked()["rules_hash"],
                         "setup rules changed: bump SETUP_ENGINE_VERSION if semantics changed, "
                         "then run scripts/lock_setup_version.py")


class SetupConfigTests(unittest.TestCase):
    def test_no_hidden_defaults(self):
        raw = json.loads(SETUP_CONFIG.read_text())
        for k in [k for k in raw if not k.startswith("_") and k != "schema"]:
            with self.subTest(missing=k), self.assertRaises(DomainError):
                SetupConfig.from_mapping({x: y for x, y in raw.items() if x != k})

    def test_invalid_values(self):
        raw = json.loads(SETUP_CONFIG.read_text())
        bad_regimes = {**raw["allowed_regimes"], "breakout": ["unknown"]}
        partial = {k: v for k, v in raw["allowed_regimes"].items() if k != "pullback"}
        for bad in ({"enabled_types": ["scalp"]}, {"enabled_types": []}, {"allowed_regimes": bad_regimes},
                    {"allowed_regimes": partial}, {"ttl_bars": 0}, {"breakout_hold_bars": True},
                    {"pullback_zone_atr": 0}, {"sweep_confirmation": "vibes"}, {"range_max_width_atr": 1.5},
                    {"range_touch_atr": 3}, {"rejection_close_fraction": 1.5}, {"atr_feature": "cvd"},
                    {"timeframes": ["15"]}):
            with self.subTest(bad=bad), self.assertRaises(DomainError):
                SetupConfig.from_mapping({**raw, **bad})

    def test_hash_and_lookback(self):
        c = SetupConfig.from_mapping(json.loads(SETUP_CONFIG.read_text()))
        self.assertEqual(c.lookback_bars, 2 * c.ttl_bars + c.recent_closed_bars)
        self.assertNotEqual(c.config_hash, c.with_(ttl_bars=13).config_hash)

    def test_no_numeric_thresholds_in_engine(self):
        tree = ast.parse((ROOT / "app" / "research" / "setup" / "engine.py").read_text())
        index_consts = {id(n.slice) for n in ast.walk(tree) if isinstance(n, ast.Subscript)}
        nums = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and id(n) not in index_consts
                and isinstance(n.value, (int, float)) and not isinstance(n.value, bool) and n.value not in (0, 1, 2)]
        self.assertEqual(nums, [])


class SetupPersistenceTests(unittest.TestCase):
    def setUp(self):
        from app.storage.setup import SQLiteSetupStore
        from tests.fakes import TempDB
        self.db = TempDB()
        self.store = SQLiteSetupStore(self.db.writer)

    def tearDown(self):
        self.db.close()

    def test_idempotent_roundtrip(self):
        s = evaluate(PATH_A, 18)
        self.assertTrue(self.store.save(s))
        self.assertEqual((self.store.last.setups_new, self.store.last.events_new), (2, 5))
        self.assertFalse(self.store.save(s))
        self.assertEqual((self.store.last.setups_new, self.store.last.events_new, self.store.last.events_known),
                         (0, 0, 5))
        self.assertEqual(self.store.load(s.key), s)

    def test_bar_by_bar_replay_records_each_fact_once(self):
        for t in range(10, 19):
            self.store.save(evaluate(PATH_A, t))
        self.assertEqual((self.store.count(), self.store.count("setups"), self.store.count("setup_events"),
                          self.store.count("setup_conflicts")), (9, 2, 5, 0))
        (b,) = [x for x in self.store.setups(ETH, M15) if x["setup_type"] == "breakout"]
        self.assertEqual([e["status"] for e in self.store.events(b["setup_id"])],
                         ["candidate", "confirmed", "invalidated"])

    def test_conflicting_result_is_recorded_not_overwritten(self):
        s = evaluate(PATH_A, 18)
        self.store.save(s)
        b = by_type(s, "breakout")[0]
        moved = replace(b, transitions=(b.transitions[0], replace(b.transitions[1], at=b.transitions[1].at +
                                                                  M15.delta)))
        later = replace(s, as_of=s.as_of + M15.delta, input_fingerprint="other", setups=(moved,))
        self.store.save(later)
        self.assertEqual(self.store.last.conflicts, 1)
        stored = self.store.events(b.setup_id)
        self.assertEqual(stored[1]["at"], b.transitions[1].at.isoformat())      # first record kept

    def test_nondeterminism_detected_and_immutable(self):
        import sqlite3
        from app.core.errors import StorageError
        s = evaluate(PATH_A, 18)
        self.store.save(s)
        with self.assertRaises(StorageError):
            self.store.save(replace(s, reason_codes=s.reason_codes + ("X",)))
        for sql in ("UPDATE setup_snapshots SET data_quality='gap'", "DELETE FROM setups",
                    "UPDATE setup_events SET status='confirmed'", "DELETE FROM setup_events"):
            with self.assertRaises(sqlite3.DatabaseError):
                self.db.conn.execute(sql)

    def test_links_to_source_snapshots(self):
        s = evaluate(PATH_R, 11, regime=regime("range"))
        self.store.save(s)
        row = self.db.conn.execute("SELECT structure_input_fingerprint, regime_input_fingerprint FROM "
                                   "setup_snapshots").fetchone()
        self.assertEqual(tuple(row), (s.structure_context["input_fingerprint"], s.regime_context["input_fingerprint"]))

    def test_migration_over_existing_database(self):
        import shutil
        import tempfile
        from pathlib import Path
        from app.storage.database import MIGRATIONS_DIR, connect, migrate
        d = Path(tempfile.mkdtemp())
        try:
            old = d / "m"
            old.mkdir()
            for f in sorted(MIGRATIONS_DIR.glob("*.sql"))[:6]:
                shutil.copy(f, old)
            c = connect(d / "x.sqlite3")
            migrate(c, old)
            self.assertEqual(migrate(c)[0], "0007_setup")
            c.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)


class SetupSafetyTests(unittest.TestCase):
    def test_no_orders_llm_network_or_sql(self):
        banned = {"submit", "place_order", "ExecutionGateway", "anthropic", "openai", "gemini", "ollama", "urlopen",
                  "requests", "sqlite3", "BybitRestClient", "UrllibTransport", "api_key"}
        for f in (ROOT / "app" / "research" / "setup").rglob("*.py"):
            tree = ast.parse(f.read_text())
            ids = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
                  {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | \
                  {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
            self.assertEqual(ids & banned, set(), f.name)

    def test_not_a_signal_statement(self):
        from app.cli.setup_cmds import DISCLAIMER
        self.assertIn("NOT trading signals", DISCLAIMER)
        self.assertIn("NOT a trading signal", (ROOT / "app" / "domain" / "setup.py").read_text())

    def test_execution_ceiling_unchanged(self):
        from app.config.settings import BUILD_EXECUTION_CEILING, ExecutionMode
        self.assertEqual(BUILD_EXECUTION_CEILING, ExecutionMode.PAPER)


def idx_close(t):
    return (t - T0) // M15.delta - W - 1
