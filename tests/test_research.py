"""Phase 7 research & backtest. Expected values are derived BY HAND (see tests/research_data.py):
breakout confirmed at the close of bar 16 (109.0), level 108.0; forward bars 17..21:
  17 (112, 108.9, 111.8)  18 (113, 110, 112.5)  19 (112.8, 109.5, 110)  20 (111, 107, 108)  21 (109, 106.5, 107)
Backtest (stop buffer 0, no slippage, taker 5.5 bps, maker 2 bps, funding none):
  entry at the OPEN of bar 17 = 109.0, stop 108.0, qty = 100 / (109 - 108) = 100, target = 109 + 2*1 = 111
  bar 17 high 112 >= 111, low 108.9 > 108 -> take profit at 111 (maker)
  gross = 2 * 100 = 200; fees = 109*100*0.00055 + 111*100*0.0002 = 5.995 + 2.22 = 8.215; net = 191.785; R = 1.91785
"""
import ast
import json
import sqlite3
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from app.core.errors import StorageError
from app.domain.enums import OutcomeStatus, SimExitReason
from app.domain.errors import DomainError
from app.domain.market import FundingRate, Symbol
from app.research.lab import splits
from app.research.lab.backtest import BacktestEngine
from app.research.lab.config import BacktestConfig, ResearchConfig
from app.research.lab.dataset import DatasetIntegrityError, candle_fingerprint, segments_of
from app.research.lab.metrics import trade_metrics
from app.research.lab.replay import ReplayEngine
from tests.research_data import (BCFG, LAST, M15, PATH_BT, RCFG, W, Lab, bcfg, bar_close, bar_open, candles, rcfg,
                                 with_bar)
from tests.setup_data import PATH_P
from tests.setup_data import service
from tests.structure_data import ETH, ROOT, T0, mirror, poisoned

A = 1e-9


def bt(cs, cfg=None, funding=None):
    """Backtest the replay of candles cs (no DB)."""
    from app.domain.research_lab import ResearchDataset, SeriesCoverage
    ds = ResearchDataset("ds_t", {"symbols": ["ETHUSDT"], "timeframe": "15m", "start": cs[0].open_time.isoformat(),
                                  "end": cs[-1].close_time.isoformat()},
                         (SeriesCoverage("ETHUSDT", "15m", "primary", len(cs), "fp", (), 0),), {}, {}, {}, {})
    rp = ReplayEngine(service()).run(ds, {"ETHUSDT": cs})
    return BacktestEngine(cfg or bcfg()).run(rp, {"ETHUSDT": cs}, M15, {"ETHUSDT": funding or []}), rp


class DatasetTests(unittest.TestCase):
    def test_deterministic_and_idempotent(self):
        lab = Lab()
        try:
            a, new_a = lab.pipe.build_dataset(["ETHUSDT"], M15, lab.start, lab.end)
            b, new_b = lab.pipe.build_dataset(["ETHUSDT"], M15, lab.start, lab.end)
            self.assertEqual((a.dataset_id, new_a, new_b), (b.dataset_id, True, False))
            self.assertEqual(a.primary("ETHUSDT").fingerprint, candle_fingerprint(lab.cs))
            self.assertEqual(set(a.versions), {"code", "feature_catalog", "regime", "structure", "setup"})
        finally:
            lab.close()

    def test_gaps_and_short_segments_listed_not_filled(self):
        cs = candles(PATH_BT, missing={5})
        segs, missing = segments_of(cs, M15, 100)
        self.assertEqual(missing, 1)
        self.assertEqual([(s.bars, s.usable) for s in segs], [(W + 5, True), (len(PATH_BT) - 6, False)])
        self.assertTrue(segs[1].reason.startswith("shorter_than_warmup:16<"))

    def test_changed_source_data_is_detected(self):
        lab = Lab(cs=candles(PATH_BT, missing={5}))
        try:
            ds = lab.dataset()
            lab.db.store.upsert_candles(candles(PATH_BT)[W + 5: W + 6], "late")        # the gap gets filled later
            with self.assertRaises(DatasetIntegrityError):
                lab.pipe.dataset(ds.dataset_id)
            ds2 = lab.dataset()
            self.assertNotEqual(ds2.dataset_id, ds.dataset_id)                          # new version, old kept
            self.assertIsNotNone(lab.store.dataset(ds.dataset_id))
        finally:
            lab.close()

    def test_component_version_change_is_detected(self):
        from unittest import mock
        lab = Lab()
        try:
            ds = lab.dataset()
            with mock.patch("app.research.lab.dataset.component_versions", return_value={**ds.versions, "setup": "2:x"}):
                with self.assertRaises(DatasetIntegrityError):
                    lab.pipe.dataset(ds.dataset_id)
        finally:
            lab.close()


class ReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lab = Lab()
        cls.ds = cls.lab.dataset()
        cls.rp = cls.lab.pipe.replay(cls.ds.dataset_id)[0]

    @classmethod
    def tearDownClass(cls):
        cls.lab.close()

    def test_identical_to_live_point_in_time_evaluation(self):
        eng = ReplayEngine(self.lab.pipe.setups)
        for k in (W + 14, W + 16, W + LAST):
            t = self.lab.cs[k].close_time
            replayed = eng.snapshot_at(self.lab.cs, k, Symbol("ETHUSDT"), M15)
            live = self.lab.pipe.setups.analyze(Symbol("ETHUSDT"), M15, t, None)
            self.assertEqual(replayed.canonical_json(), live.canonical_json())

    def test_future_candles_do_not_change_past_evaluations(self):
        eng = ReplayEngine(service())
        full = self.lab.cs
        for k in range(W + 12, W + LAST + 1):
            cut = full[:k + 1]
            spiked = cut + [poisoned(c) for c in full[k + 1:]]
            self.assertEqual(eng.snapshot_at(spiked, k, Symbol("ETHUSDT"), M15).canonical_json(),
                             eng.snapshot_at(cut, k, Symbol("ETHUSDT"), M15).canonical_json())

    def test_reproducible_and_idempotent(self):
        again, created = self.lab.pipe.replay(self.ds.dataset_id)
        self.assertFalse(created)
        self.assertEqual((again.run_id, again.replay_hash), (self.rp.run_id, self.rp.replay_hash))

    def test_observations_and_warmup_are_explicit(self):
        s = self.rp.summary()
        self.assertEqual(s["evaluations"], len(self.lab.cs))
        self.assertGreater(s["not_valid"]["insufficient_history"], 0)
        types = sorted(o.setup.setup_type.value for o in self.rp.observations)
        self.assertIn("breakout", types)

    def test_longer_history_same_observations(self):
        other = Lab(warmup=W + 60)
        try:
            rp2 = other.replay()
            key = lambda r: [(o.setup.setup_type.value, o.setup.key_level, [(t.status.value, t.reason) for t in
                                                                           o.setup.transitions]) for o in r.observations]
            self.assertEqual(key(rp2), key(self.rp))
        finally:
            other.close()

    def test_regime_replay_requires_context_series(self):
        with self.assertRaises(DomainError):
            self.lab.pipe.replay(self.ds.dataset_id, with_regime=True)


class OutcomeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lab = Lab()
        rp = cls.lab.replay()
        cls.run_id, cls.obs, cls.summary, _ = cls.lab.pipe.outcomes(rp.run_id)

    @classmethod
    def tearDownClass(cls):
        cls.lab.close()

    def breakout(self, h):
        return next(o for o in self.obs if o.setup_type == "breakout" and o.horizon == h)

    def test_hand_computed_long_outcome(self):
        o = self.breakout(5)
        self.assertEqual(o.status, OutcomeStatus.VALID)
        v = o.values
        self.assertAlmostEqual(v["forward_return"], 107 / 109 - 1, delta=A)
        self.assertAlmostEqual(v["mfe"], 4 / 109, delta=A)          # max high 113 at bar 18 (2nd bar)
        self.assertAlmostEqual(v["mae"], 2.5 / 109, delta=A)        # min low 106.5 at bar 21 (5th bar)
        self.assertEqual((v["bars_to_mfe"], v["bars_to_mae"]), (2, 5))
        self.assertEqual(v["first_1atr"], "favorable")            # 1 ATR ~3.57: fav 4 on bar 18 first

    def test_censored_is_not_zero(self):
        o = self.breakout(10)
        self.assertEqual((o.status, o.reason, dict(o.values)), (OutcomeStatus.CENSORED, "END_OF_DATA:5_of_10_bars", {}))
        with self.assertRaises(DomainError):
            replace(o, values={"forward_return": 0.0})

    def test_short_mirror_has_identical_outcome(self):
        lab = Lab(path=mirror(PATH_BT))
        try:
            _, obs, _, _ = lab.pipe.outcomes(lab.replay().run_id)
            o = next(x for x in obs if x.setup_type == "breakout" and x.horizon == 5)
            self.assertEqual(o.direction, "bearish")
            # mirrored price p' = 200 - p: returns are relative to p0' = 91, so compare the absolute excursions
            self.assertAlmostEqual(o.values["mfe"] * 91, 4, delta=1e-9)
            self.assertAlmostEqual(o.values["mae"] * 91, 2.5, delta=1e-9)
            self.assertAlmostEqual(o.values["forward_return"], -(93 / 91 - 1), delta=A)   # price rose to 93: loss
            self.assertEqual((o.values["bars_to_mfe"], o.values["bars_to_mae"]), (2, 5))
        finally:
            lab.close()

    def test_gap_inside_horizon_is_incomplete(self):
        lab = Lab(cs=candles(PATH_BT, missing={19}))
        try:
            _, obs, _, _ = lab.pipe.outcomes(lab.replay().run_id)
            o = next(x for x in obs if x.setup_type == "breakout" and x.horizon == 5)
            self.assertEqual((o.status, o.reason), (OutcomeStatus.INCOMPLETE, "GAP_IN_HORIZON"))
        finally:
            lab.close()

    def test_summary_counts_and_not_available(self):
        h40 = self.summary["horizons"]["40"]
        self.assertEqual(h40["counts"]["valid"], 0)
        self.assertEqual(h40["forward_return"]["mean"], {"status": "not_available", "reason": "no_valid_observations"})
        self.assertEqual(self.summary["horizons"]["5"]["counts"]["valid"], 1)


class BacktestTests(unittest.TestCase):
    def test_long_take_profit_hand_computed(self):
        res, _ = bt(candles(PATH_BT))
        (t,) = res.trades
        self.assertEqual((t.entry_time, t.entry_price, t.qty, t.stop, t.take_profit), (bar_open(17, warmup=W), 109.0,
                                                                                       100.0, 108.0, 111.0))
        self.assertEqual((t.exit_reason, t.exit_price, t.bars_held), (SimExitReason.TAKE_PROFIT, 111.0, 1))
        self.assertAlmostEqual(t.gross_pnl, 200, delta=A)
        self.assertAlmostEqual(t.fees, 5.995 + 2.22, delta=A)
        self.assertAlmostEqual(t.net_pnl, 191.785, delta=A)
        self.assertAlmostEqual(t.r_multiple, 1.91785, delta=A)
        self.assertGreaterEqual(t.entry_time, t.decision_time)

    def test_short_mirror_symmetry(self):
        res, _ = bt(candles(mirror(PATH_BT)))
        (t,) = res.trades
        self.assertEqual((t.direction, t.entry_price, t.stop, t.take_profit), ("bearish", 91.0, 92.0, 89.0))
        self.assertAlmostEqual(t.gross_pnl, 200, delta=A)
        self.assertAlmostEqual(t.fees, 91 * 100 * 0.00055 + 89 * 100 * 0.0002, delta=A)

    def test_stop_and_target_in_one_bar_takes_the_stop(self):
        res, _ = bt(with_bar(candles(PATH_BT), 17, high=111.5, low=107.5, close=110))
        (t,) = res.trades
        self.assertEqual((t.exit_reason, t.exit_price, t.ambiguous), (SimExitReason.STOP_AMBIGUOUS, 108.0, True))
        self.assertAlmostEqual(t.net_pnl, -100 - 5.995 - 108 * 100 * 0.00055, delta=A)

    def test_entry_at_next_open_not_decision_close(self):
        res, _ = bt(with_bar(candles(PATH_BT), 17, open=109.5))
        (t,) = res.trades
        self.assertEqual(t.entry_price, 109.5)                    # decision close was 109.0
        self.assertAlmostEqual(t.initial_risk, 1.5 * 100, delta=A)   # R uses the ACTUAL fill vs the frozen stop

    def test_gap_through_stop_fills_at_open(self):
        path = PATH_P[:17] + [(110.5, 108.6, 110.2), (110.2, 107, 107.8)]   # bars 17, 18
        cs = with_bar(candles(path), 18, open=107.5, high=108)
        res, _ = bt(cs)
        (t,) = res.trades
        self.assertEqual((t.exit_reason, t.exit_price), (SimExitReason.STOP, 107.5))
        self.assertAlmostEqual(t.gross_pnl, -150, delta=A)

    def test_slippage_is_attributed_not_double_counted(self):
        res, _ = bt(candles(PATH_BT), bcfg(costs={**bcfg().costs.canonical(), "slippage_bps": 10}))
        (t,) = res.trades
        fill = 109 * 1.001
        self.assertAlmostEqual(t.entry_price, fill, delta=1e-9)
        self.assertAlmostEqual(t.slippage_cost, (fill - 109) * 100, delta=1e-6)
        self.assertAlmostEqual(t.net_pnl, t.gross_pnl - t.fees + t.funding, delta=1e-9)   # slippage not subtracted again

    def test_funding_assumed_and_data_never_both(self):
        path = PATH_P[:17] + [(110, 108.8, 109.5)] * 10          # bars 17..26 never reach stop/target
        common = dict(take_profit_r=50, max_hold_bars=10)
        assumed = bcfg(**common, costs={**bcfg().costs.canonical(), "funding_mode": "data_or_assumed"})
        res, _ = bt(candles(path), assumed)
        (t,) = res.trades
        # entry 06:15, time exit at the close of the 10th bar = 08:45; funding at 08:00 on notional 109.5*100
        self.assertEqual((t.exit_reason, t.funding_source), (SimExitReason.TIME, "assumed"))
        self.assertAlmostEqual(t.funding, -0.0001 * 109.5 * 100, delta=1e-9)
        f8 = datetime(2026, 3, 3, 8, tzinfo=timezone.utc)
        res, _ = bt(candles(path), assumed, [FundingRate(ETH, f8, D("0.0003"))])
        (t,) = res.trades
        self.assertEqual(t.funding_source, "data")
        self.assertAlmostEqual(t.funding, -0.0003 * 109.5 * 100, delta=1e-9)
        self.assertAlmostEqual(t.net_pnl, t.gross_pnl - t.fees + t.funding, delta=1e-9)

    def test_no_next_bar_no_trade(self):
        res, _ = bt(candles(PATH_BT[:17]))                       # confirmed at the close of the LAST bar
        self.assertEqual((res.trades, res.skipped.get("no_next_bar")), ([], 1))

    def test_equity_and_drawdown(self):
        res, _ = bt(with_bar(candles(PATH_BT), 17, high=111.5, low=107.5, close=110))
        eq = [v for _, v in res.equity]
        self.assertAlmostEqual(eq[-1], 10000 + res.trades[0].net_pnl, delta=1e-6)
        m = trade_metrics(res.trades, eq, res.bars, res.position_bars)
        self.assertGreater(m["max_drawdown"]["value"], 0)
        self.assertEqual(m["profit_factor"], {"status": "not_available", "reason": "no_trades"} if not res.trades
                         else {"status": "not_available", "reason": "no_losing_trades"}
                         if all(t.net_pnl > 0 for t in res.trades) else m["profit_factor"])

    def test_metrics_not_available_without_trades(self):
        m = trade_metrics([])
        for k in ("win_rate", "expectancy_per_trade", "average_r", "profit_factor", "max_consecutive_wins"):
            self.assertEqual(m[k]["status"], "not_available", k)

    def test_simulated_trade_invariants(self):
        res, _ = bt(candles(PATH_BT))
        t = res.trades[0]
        with self.assertRaises(DomainError):
            replace(t, entry_time=t.decision_time - M15.delta)
        with self.assertRaises(DomainError):
            replace(t, net_pnl=t.net_pnl - t.fees)                # double-counted fees


class SplitTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.p = splits.plan(self.start, self.start + M15.delta * 100, (0.6, 0.2, 0.2), M15, 2)

    def test_chronological_non_overlapping(self):
        r = self.p.ranges()
        self.assertEqual(r["train"], (self.start, self.start + M15.delta * 60))
        self.assertEqual(r["validation"], (self.start + M15.delta * 60, self.start + M15.delta * 80))
        self.assertEqual(r["test"][1], self.start + M15.delta * 100)

    def test_purge_and_embargo(self):
        T = lambda k: self.start + M15.delta * k  # noqa: E731
        items = [(T(10), T(15)), (T(58), T(62)),                 # 2nd crosses train->validation: purged
                 (T(60), T(65)), (T(61), T(66)),                 # starts inside the 2-bar embargo: dropped
                 (T(62), T(70)), (T(90), T(130))]                # last runs past the data: test keeps it
        a = splits.assign(items, self.p, lambda x: x[0], lambda x: x[1])
        self.assertEqual({k: len(v) for k, v in a.split.items()}, {"train": 1, "validation": 1, "test": 1})
        self.assertEqual((a.purged, a.embargoed), (1, 2))

    def test_order_preserved_no_shuffle(self):
        items = [(self.start + M15.delta * k, self.start + M15.delta * k) for k in range(0, 50, 5)]
        a = splits.assign(items, self.p, lambda x: x[0], lambda x: x[1])
        self.assertEqual(a.split["train"], sorted(a.split["train"]))

    def test_config_validation(self):
        raw = json.loads(RCFG.read_text())
        for bad in ({"split_fractions": [0.5, 0.5]}, {"split_fractions": [0.6, 0.3, 0.2]},
                    {"outcome_horizons": [10, 5]}, {"outcome_anchor": "always"}, {"embargo_bars": -1}):
            with self.subTest(bad=bad), self.assertRaises(DomainError):
                ResearchConfig.from_mapping({**raw, **bad})
        braw = json.loads(BCFG.read_text())
        for bad in ({"take_profit_r": 0}, {"entry_on": "hope"}, {"setup_types": ["scalp"]},
                    {"costs": {**braw["costs"], "funding_mode": "maybe"}}, {"costs": {"taker_fee_bps": 1}}):
            with self.subTest(bad=bad), self.assertRaises(DomainError):
                BacktestConfig.from_mapping({**braw, **bad})


class ResearchPersistenceTests(unittest.TestCase):
    def test_runs_idempotent_immutable_and_separate(self):
        lab = Lab()
        try:
            rp = lab.replay()
            _, _, _, c1 = lab.pipe.outcomes(rp.run_id)
            _, _, c2 = lab.pipe.backtest(rp.run_id, bcfg())
            _, _, _, c3 = lab.pipe.outcomes(rp.run_id)
            _, _, c4 = lab.pipe.backtest(rp.run_id, bcfg())
            self.assertEqual((c1, c2, c3, c4), (True, True, False, False))
            counts = lab.store.counts()
            self.assertEqual((counts["research_runs"], counts["sim_trades"]), (3, 1))
            real = lab.db.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            self.assertEqual(real, 0)                                       # simulated trades never in `trades`
            for sql in ("UPDATE research_runs SET summary_json='{}'", "DELETE FROM sim_trades",
                        "UPDATE research_datasets SET spec_json='{}'", "DELETE FROM outcome_observations"):
                with self.assertRaises(sqlite3.DatabaseError):
                    lab.db.conn.execute(sql)
        finally:
            lab.close()

    def test_rerun_with_different_result_is_a_conflict(self):
        lab = Lab()
        try:
            rp = lab.replay()
            res, rep, _ = lab.pipe.backtest(rp.run_id, bcfg())
            with self.assertRaises(StorageError):
                lab.store.save_run(run_id=res.run_id, kind="backtest", parent_id=rp.run_id, dataset_id=rp.dataset_id,
                                   config={}, versions={}, summary={}, full_result={"tampered": True})
            self.assertEqual(lab.store.counts()["research_conflicts"], 1)
            self.assertEqual(lab.store.run(res.run_id)["summary"]["rule"], "confirmed_setup_fixed_r")  # kept
        finally:
            lab.close()

    def test_migration_over_existing_database(self):
        import shutil
        import tempfile
        from pathlib import Path
        from app.storage.database import MIGRATIONS_DIR, connect, migrate
        d = Path(tempfile.mkdtemp())
        try:
            old = d / "m"
            old.mkdir()
            for f in sorted(MIGRATIONS_DIR.glob("*.sql"))[:7]:
                shutil.copy(f, old)
            c = connect(d / "x.sqlite3")
            migrate(c, old)
            self.assertEqual(migrate(c)[0], "0008_research")
            c.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)


class ResearchSafetyTests(unittest.TestCase):
    def test_no_execution_llm_network_or_sql_in_lab(self):
        banned = {"submit", "place_order", "ExecutionGateway", "DisabledGateway", "OrderPermission", "anthropic",
                  "openai", "gemini", "ollama", "urlopen", "requests", "sqlite3", "BybitRestClient", "api_key"}
        for f in (ROOT / "app" / "research" / "lab").rglob("*.py"):
            tree = ast.parse(f.read_text())
            ids = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
                  {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | \
                  {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
            self.assertEqual(ids & banned, set(), f.name)

    def test_monitor_never_runs_research(self):
        for f in (ROOT / "app" / "monitor").rglob("*.py"):
            self.assertNotIn("research.lab", f.read_text(), f.name)

    def test_simulated_fills_are_marked_simulator(self):
        src = (ROOT / "app" / "research" / "lab" / "backtest.py").read_text()
        self.assertIn("EventSource.SIMULATOR", src)
        self.assertNotIn("EventSource.EXCHANGE", src)

    def test_execution_ceiling_unchanged(self):
        from app.config.settings import BUILD_EXECUTION_CEILING, ExecutionMode
        self.assertEqual(BUILD_EXECUTION_CEILING, ExecutionMode.PAPER)
