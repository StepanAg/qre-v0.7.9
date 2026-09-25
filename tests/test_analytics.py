"""Phase 8 analytics. Expected numbers are computed BY HAND in comments/docstrings;
tests never recompute a metric with the formula under test."""
import sqlite3
import unittest

from app.analytics import sources
from app.domain.research_lab import SimulatedTrade
from tests.analytics_data import RUN, AnalyticsLab, trade


class ReadOnlyFoundationTests(unittest.TestCase):
    """Step 2: read-only reader, source registry."""

    def test_reader_returns_domain_objects(self):
        lab = AnalyticsLab([trade(1, 50.0), trade(2, -30.0)])
        try:
            r = lab.reader()
            ts = r.trades(RUN)
            self.assertEqual([type(t) for t in ts], [SimulatedTrade, SimulatedTrade])
            self.assertEqual([t.net_pnl for t in ts], [50.0, -30.0])
            self.assertEqual(r.run(RUN)["config"]["initial_equity_quote"], 10000.0)
            self.assertEqual(r.dataset("ds_fixture")["spec"]["timeframe"], "15m")
            self.assertEqual(r.migrations()[-1], "0008_research")
            r.close()
        finally:
            lab.close()

    def test_connection_cannot_write(self):
        lab = AnalyticsLab([trade(1, 50.0)])
        try:
            r = lab.reader()
            with self.assertRaises(sqlite3.OperationalError):          # mode=ro: "attempt to write a readonly database"
                r.conn.execute("INSERT INTO research_conflicts(run_id, stored_hash, new_hash, created_at) "
                               "VALUES ('x','a','b','c')")
            r.close()
        finally:
            lab.close()

    def test_registry_statuses(self):
        self.assertIsNone(sources.require("research.trades"))
        for sid in ("ai.decisions", "risk.decisions", "market.quotes", "exec.post_exit", "real.trades"):
            self.assertEqual(sources.require(sid), {"status": "not_available", "reason": "source_not_available"}, sid)
        self.assertEqual(sources.get("real.trades").status, sources.SCHEMA_ONLY)
        with self.assertRaises(LookupError):
            sources.get("telegram")

    def test_availability_reports_missing_tables(self):
        rows = {x["source"]: x["status"] for x in sources.availability({"sim_trades"})}
        self.assertEqual(rows["research.equity"], "source_not_available:missing_tables")
        self.assertEqual(rows["ai.decisions"], "source_not_available")


class DistributionTests(unittest.TestCase):
    """Step 3: percentiles (linear interpolation) and D2 sample rules."""

    def test_percentiles_by_hand(self):
        from app.research.lab import stats
        xs = [4, 1, 3, 2]                       # sorted 1,2,3,4: p25 rank 0.75 -> 1.75; p75 rank 2.25 -> 3.25
        self.assertEqual(stats.percentile(xs, 25, "x"), {"value": 1.75})
        self.assertEqual(stats.percentile(xs, 75, "x"), {"value": 3.25})
        self.assertEqual(stats.percentile([7], 50, "x"), {"value": 7})
        self.assertEqual(stats.percentile([], 50, "x"), {"status": "not_available", "reason": "no_valid_x"})
        with self.assertRaises(ValueError):
            stats.percentile(xs, 101, "x")

    def test_d2_thresholds(self):
        from app.analytics.distributions import distribution
        d3 = distribution([1, 2, 3], "r")
        self.assertEqual(d3["p25"]["reason"], "sample_too_small_for:p25(n=3<4)")
        self.assertEqual(d3["p10"]["reason"], "sample_too_small_for:p10(n=3<20)")
        self.assertEqual((d3["median"], d3["min"], d3["max"]), ({"value": 2}, {"value": 1.0}, {"value": 3.0}))
        self.assertEqual(d3["warning"], "Sample size = 3. Statistics are not statistically robust.")
        d4 = distribution([1, 2, 3, 4], "r")
        self.assertEqual(d4["p25"], {"value": 1.75})
        d20 = distribution(list(range(1, 21)), "r")      # 1..20: p10 rank 1.9 -> 2.9; p90 rank 17.1 -> 18.1
        self.assertAlmostEqual(d20["p10"]["value"], 2.9)
        self.assertAlmostEqual(d20["p90"]["value"], 18.1)
        self.assertIn("warning", d20)                    # 20 < 30
        self.assertNotIn("warning", distribution(list(range(30)), "r"))

    def test_empty_is_not_available_never_zero(self):
        from app.analytics.distributions import distribution
        d = distribution([], "pnl")
        for k in ("mean", "median", "p25", "p75", "p10", "p90", "min", "max"):
            self.assertEqual(d[k]["status"], "not_available", k)


class PerformanceRiskCostTests(unittest.TestCase):
    """Step 4 (TZ 5.1-5.3). Fixture: nets +50, -30, +20, 0; fees 1; risk 100 -> R 0.5, -0.3, 0.2, 0.
    2 wins, 1 loss, 1 breakeven; net 40; price gross 51-29+21+1 = 44; gross profit 70, gross loss -30;
    PF 70/30; payoff (70/2)/(30/1) = 35/30; expectancy 10; median trade (0+20)/2 = 10; R mean 0.1 median 0.1."""

    def setUp(self):
        from app.analytics import engine
        self.e = engine
        self.ts = [trade(1, 50.0), trade(2, -30.0), trade(3, 20.0), trade(4, 0.0)]

    def v(self, m, k):
        return m["metrics"][k]["value"]

    def test_hand_computed_performance(self):
        p = self.e.performance(self.ts, 10000.0)
        self.assertEqual([self.v(p, k) for k in ("trades", "wins", "losses", "breakeven")], [4, 2, 1, 1])
        self.assertAlmostEqual(self.v(p, "net_pnl"), 40)
        self.assertAlmostEqual(self.v(p, "price_gross_pnl"), 44)
        self.assertAlmostEqual(self.v(p, "gross_profit"), 70)
        self.assertAlmostEqual(self.v(p, "gross_loss"), -30)
        self.assertAlmostEqual(self.v(p, "profit_factor"), 70 / 30)
        self.assertAlmostEqual(self.v(p, "payoff_ratio"), 35 / 30)
        self.assertAlmostEqual(self.v(p, "expectancy"), 10)
        self.assertAlmostEqual(self.v(p, "median_trade"), 10)
        self.assertEqual((self.v(p, "best_trade"), self.v(p, "worst_trade")), (50.0, -30.0))
        self.assertEqual((self.v(p, "win_rate"), self.v(p, "breakeven_rate")), (0.5, 0.25))   # breakeven is no loss
        self.assertAlmostEqual(self.v(p, "expectancy_r"), 0.1)
        self.assertAlmostEqual(self.v(p, "median_r"), 0.1)
        self.assertAlmostEqual(self.v(p, "return_pct"), 0.004)
        self.assertEqual(p["warnings"], ["Sample size = 4. Statistics are not statistically robust."])

    def test_zero_one_all_winners_all_losers(self):
        z = self.e.performance([])["metrics"]
        for k in ("net_pnl", "win_rate", "expectancy", "profit_factor", "payoff_ratio", "median_r", "return_pct"):
            self.assertEqual(z[k]["status"], "not_available", k)          # never 0 / 0% / infinity
        self.assertEqual(z["trades"], {"value": 0})
        one = self.e.performance([trade(1, 25.0)])["metrics"]
        self.assertEqual((one["net_pnl"]["value"], one["median_trade"]["value"]), (25.0, 25.0))
        winners = self.e.performance([trade(1, 5.0), trade(2, 7.0)])["metrics"]
        self.assertEqual(winners["profit_factor"], {"status": "not_available", "reason": "no_losing_trades"})
        self.assertEqual(winners["payoff_ratio"]["reason"], "no_losing_trades")
        losers = self.e.performance([trade(1, -5.0), trade(2, -7.0)])["metrics"]
        self.assertEqual(losers["profit_factor"], {"value": 0.0})              # genuinely zero: no profit at all
        self.assertEqual(losers["payoff_ratio"]["reason"], "no_winning_trades")
        self.assertEqual(losers["win_rate"], {"value": 0.0})

    def test_outliers_mask_typical_trade(self):
        ts = [trade(1, 500.0)] + [trade(k, -10.0) for k in range(2, 6)]      # net 460 > 0, median R -0.1
        self.assertIn("WARNING: outliers_mask_typical_trade (net > 0 but median R < 0)",
                      self.e.performance(ts)["warnings"])

    def test_cumulative_return_from_equity(self):
        from tests.analytics_data import T0, M15
        p = self.e.performance(self.ts, 10000.0, [(T0, 10000.0), (T0 + M15, 10250.0)])
        self.assertAlmostEqual(self.v(p, "cumulative_return"), 0.025)
        self.assertEqual(self.e.performance(self.ts, 10000.0, [(T0, 1.0)])["metrics"]["cumulative_return"]["reason"],
                         "equity_curve_needs_2_points")

    def test_risk_and_zero_initial_risk(self):
        ts = self.ts + [trade(5, 10.0, risk=0.0)]                            # R cannot exist
        r = self.e.risk(ts)["metrics"]
        self.assertEqual(r["r_not_available"], {"value": 1})
        self.assertEqual(r["r_distribution"]["n"], 4)
        self.assertAlmostEqual(r["mean_r"]["value"], 0.1)
        self.assertAlmostEqual(r["stdev_r"]["value"], 0.3366501646120693)  # stdev of 0.5,-0.3,0.2,0 (sample)
        self.assertEqual(self.e.risk([trade(1, 5.0)])["metrics"]["stdev_r"]["status"], "not_available")

    def test_costs_decomposition_no_double_counting(self):
        # fees 2, funding -0.5 (paid), slippage 3, net 10 -> price gross 12.5; theoretical 15.5;
        # cost share (2 + 3 + 0.5) / 15.5
        c = self.e.costs([trade(1, 10.0, fees=2.0, funding=-0.5, slippage=3.0)])["metrics"]
        self.assertAlmostEqual(c["price_gross"]["value"], 12.5)
        self.assertAlmostEqual(c["theoretical_gross"]["value"], 15.5)
        self.assertAlmostEqual(c["net"]["value"], 10.0)
        self.assertAlmostEqual(c["price_gross"]["value"] - c["fees"]["value"] + c["funding"]["value"],
                               c["net"]["value"])                             # slippage NOT subtracted again
        self.assertAlmostEqual(c["cost_share"]["value"], 5.5 / 15.5)
        self.assertEqual(c["funding_source_mix"], {"value": {"none": 1}})
        self.assertEqual(c["pnl_inconsistent_trades"], {"value": []})
        self.assertFalse(self.e.pnl_consistent(12.5, 2.0, -0.5, 11.0))


class DrawdownTests(unittest.TestCase):
    """Step 5 (TZ 5.8). Equity 100, 110, 105, 99, 111, 108 at 15-minute steps t0..t5:
    episode 1: peak 110@t1 -> trough 99@t3 (depth 11, 10%) -> recovery t4 (111): duration 45m,
               to trough 30m, recovery 15m; episode 2: peak 111@t4 -> 108@t5, depth 3 (3/111), not recovered."""

    def setUp(self):
        from tests.analytics_data import T0, M15
        self.T0, self.M = T0, M15
        self.pts = [(T0 + M15 * k, v) for k, v in enumerate([100, 110, 105, 99, 111, 108])]

    def test_episodes_by_hand(self):
        from app.analytics import drawdown
        d = drawdown.analyze(self.pts)
        e1, e2 = d["episodes"]
        self.assertEqual((e1["peak"], e1["trough"], e1["depth"]), (110, 99, 11))
        self.assertAlmostEqual(e1["depth_pct"], 0.1)
        self.assertEqual((e1["start"], e1["trough_time"], e1["recovery"]),
                         tuple((self.T0 + self.M * k).isoformat() for k in (1, 3, 4)))
        self.assertEqual((e1["duration_s"], e1["time_to_trough_s"], e1["recovery_duration_s"]),
                         ({"value": 2700.0}, 1800.0, {"value": 900.0}))
        self.assertEqual((e2["depth"], e2["recovery"]), (3, None))
        self.assertEqual(e2["duration_s"], {"status": "not_available", "reason": "not_recovered"})
        self.assertEqual((d["max_drawdown"], d["current_drawdown"]), ({"value": 11}, {"value": 3}))
        self.assertAlmostEqual(d["max_drawdown_pct"]["value"], 0.1)
        self.assertEqual(d["longest_duration_s"], {"value": 2700.0})

    def test_monotonic_and_short_series(self):
        from app.analytics import drawdown
        up = drawdown.analyze([(self.T0 + self.M * k, 100 + k) for k in range(4)])
        self.assertEqual((up["max_drawdown"], up["episodes"]), ({"value": 0.0}, []))
        self.assertEqual(up["longest_duration_s"]["reason"], "no_recovered_episode")
        self.assertEqual(drawdown.analyze(self.pts[:1])["max_drawdown"]["reason"], "equity_curve_needs_2_points")

    def test_curves_and_risk_integration(self):
        from app.analytics import engine
        ts = [trade(2, -30.0), trade(1, 50.0)]                   # exit order t1 then t2 (entries 10/20 bars)
        c = engine.curves(ts, self.pts)
        self.assertEqual([p["cumulative_r"] for p in c["cumulative_r"]], [0.5, 0.2])
        self.assertEqual([p["drawdown"] for p in c["drawdown"]], [0, 0, 5, 11, 0, 3])
        r = engine.risk(ts, self.pts)["metrics"]
        self.assertEqual((r["max_drawdown"], r["max_drawdown_duration_s"], r["max_drawdown_recovery_s"]),
                         ({"value": 11}, {"value": 2700.0}, {"value": 900.0}))


def _bars(rows, start_bar=10, symbol="ETHUSDT"):
    from decimal import Decimal as D
    from app.domain.market import Candle, Symbol, Timeframe
    from tests.analytics_data import M15, T0
    return [Candle(Symbol(symbol), Timeframe.M15, T0 + M15 * (start_bar + k), D(str(o)), D(str(h)), D(str(l)),
                   D(str(c)), D("1"), D(str(c))) for k, (o, h, l, c) in enumerate(rows)]


class ExcursionExitTests(unittest.TestCase):
    """Step 6 (TZ 5.4-5.5). Long: entry 100 (open of bar 10), stop 99, qty 100; bars
    b1 (100, 101, 99.5, 100.8), b2 (100.8, 103, 100.5, 102); exit 102 -> gross 200, net 199 (fee 1).
    MFE 3 (3%, 3R) on bar 2; MAE 0.5 (0.5R) on bar 1 -> adverse first; capture 2/3;
    entry efficiency 1 - 0.5/3.5; exit efficiency (102 - 99.5)/(103 - 99.5)."""
    LONG = [(100, 101, 99.5, 100.8), (100.8, 103, 100.5, 102)]

    def check(self, r):
        from app.analytics.excursions import trade_excursions  # noqa: F401
        self.assertEqual(r["status"], "valid")
        self.assertAlmostEqual(r["mfe_price"], 3)
        self.assertAlmostEqual(r["mae_price"], 0.5)
        self.assertAlmostEqual(r["mfe_pct"], 0.03)
        self.assertAlmostEqual(r["mfe_r"]["value"], 3)
        self.assertAlmostEqual(r["mae_r"]["value"], 0.5)
        self.assertEqual((r["bars_to_mfe"], r["bars_to_mae"], r["adverse_first"]), (2, 1, True))
        self.assertAlmostEqual(r["capture_ratio"]["value"], 2 / 3)
        self.assertAlmostEqual(r["entry_efficiency"]["value"], 1 - 0.5 / 3.5)
        self.assertAlmostEqual(r["exit_efficiency"]["value"], 2.5 / 3.5)
        self.assertTrue(r["bar_resolution_upper_bound"])

    def test_long_by_hand(self):
        from app.analytics.excursions import trade_excursions
        from app.domain.market import Timeframe
        t = trade(1, 199.0)
        self.assertAlmostEqual(t.exit_price, 102.0)
        self.check(trade_excursions(t, _bars(self.LONG), Timeframe.M15))

    def test_short_mirror_same_values(self):
        from app.analytics.excursions import trade_excursions
        from app.domain.market import Timeframe
        mirror = [(200 - o, 200 - l, 200 - h, 200 - c) for o, h, l, c in self.LONG]
        t = trade(1, 199.0, direction="bearish")
        self.assertAlmostEqual(t.exit_price, 98.0)
        self.check(trade_excursions(t, _bars(mirror), Timeframe.M15))

    def test_gap_in_window(self):
        from app.analytics.excursions import trade_excursions
        from app.domain.market import Timeframe
        r = trade_excursions(trade(1, 199.0), _bars(self.LONG[:1]), Timeframe.M15)
        self.assertEqual((r["status"], r["reason"]), ("not_available", "gap_in_trade_window"))

    def test_summary_and_exits(self):
        from app.analytics import engine
        from app.domain.enums import SimExitReason as X
        from app.domain.market import Timeframe
        bars = _bars(self.LONG)
        ts = [trade(1, 199.0), trade(2, -101.0, reason=X.STOP), trade(3, 20.0, reason=X.END_OF_DATA)]
        exc = engine.excursions(ts, lambda s, a, b: [c for c in bars if a <= c.open_time < b], Timeframe.M15)
        self.assertEqual((exc["summary"]["valid"], exc["summary"]["not_available"]), (1, 2))  # only trade 1 has bars
        self.assertEqual(exc["summary"]["late_entry"]["reason"], "source_not_available")
        g = engine.exits(ts, exc["trades"])["groups"]
        self.assertEqual(list(g), ["stop", "take_profit", "end_of_data"])
        self.assertEqual((g["stop"]["count"], g["stop"]["median_r"]), ({"value": 1}, {"value": -1.01}))
        self.assertAlmostEqual(g["take_profit"]["median_mfe_pct"]["value"], 0.03)
        self.assertEqual(g["stop"]["median_mae_pct"]["reason"], "no_valid_excursions")
        self.assertIn("not closed by the rule", g["end_of_data"]["note"])
        self.assertEqual(g["take_profit"]["median_holding_s"], {"value": 1800.0})


def regime_snapshot(kind: str, as_of, symbol="ETHUSDT"):
    """A real Phase 3 RegimeSnapshot re-dated to `as_of` (the key includes as_of)."""
    from dataclasses import replace
    from app.domain.market import Symbol, Timeframe
    from app.research.regime.engine import RegimeEngine
    from tests.regime_data import atr_units, cfg, snap
    feats = {"up": atr_units(spread=2, slope=1, disp=1, ret=2), "range": atr_units(spread=0.3, slope=0.1, disp=0.2)}
    rs = RegimeEngine(cfg()).classify(Symbol(symbol), snap(feats[kind], symbol=Symbol(symbol)), Timeframe.M15,
                                      (Timeframe.M15,))
    return replace(rs, as_of=as_of)


class GroupingPortfolioPeriodTests(unittest.TestCase):
    """Step 7 (TZ 5.6, 5.10, 5.11)."""

    def test_groups_by_hand(self):
        from app.analytics import engine
        ts = [trade(1, 50.0), trade(2, -30.0, direction="bearish"), trade(3, 20.0, setup_type="pullback", bars=3)]
        g = engine.group_trades(ts, lambda t: t.setup_type)
        # breakout: +50, -30 -> expectancy 10, PF 50/30; exit order +50 then -30 -> sequence drawdown 30
        self.assertEqual(g["breakout"]["trades"], {"value": 2})
        self.assertAlmostEqual(g["breakout"]["expectancy"]["value"], 10)
        self.assertAlmostEqual(g["breakout"]["profit_factor"]["value"], 50 / 30)
        self.assertEqual(g["breakout"]["trade_sequence_drawdown"], {"value": 30.0})
        self.assertEqual(g["pullback"]["profit_factor"]["reason"], "no_losing_trades")
        self.assertIn("warning", g["pullback"])
        h = engine.group_trades(ts, engine.holding_bucket)          # 2 bars = 30m -> "<=30m"; 3 bars = 45m
        self.assertEqual({k: v["trades"]["value"] for k, v in h.items()}, {"30m-6h": 1, "<=30m": 2})

    def test_filters(self):
        from app.analytics.filters import AnalyticsFilter as F
        from tests.analytics_data import M15, T0
        ts = [trade(1, 50.0), trade(2, -30.0, direction="bearish"), trade(3, 20.0, setup_type="pullback")]
        fit = {"su_1": "allowed", "su_2": "not_allowed"}
        kw = dict(timeframe="15m", regime_fit_of=fit)
        self.assertEqual([t.trade_no for t in F(direction="bullish").trades(ts, **kw)], [1, 3])
        self.assertEqual([t.trade_no for t in F(regime_fit="unknown").trades(ts, **kw)], [3])   # no observation
        self.assertEqual([t.trade_no for t in F(start=T0 + M15 * 15, end=T0 + M15 * 25).trades(ts, **kw)], [2])
        self.assertEqual(F(timeframe="1h").trades(ts, **kw), [])
        for bad in (dict(direction="long"), dict(regime_fit="maybe")):
            with self.assertRaises(ValueError):
                F(**bad)
        with self.assertRaises(ValueError):
            F(regime="ranging").trades(ts, **kw)                     # D1 = NO: research has no regime label
        with self.assertRaises(ValueError):
            F(exit_reason="stop").live_rows([], "setup_time")
        from datetime import datetime
        with self.assertRaises(ValueError):
            F(start=datetime(2026, 1, 1))                            # naive timestamp

    def test_portfolio_by_hand(self):
        from app.analytics import engine
        ts = [trade(1, 10.0, entry_bar=0, bars=4), trade(2, -5.0, entry_bar=2, bars=4),
              trade(3, 2.0, entry_bar=8, bars=1, direction="bearish")]
        p = engine.portfolio(ts)
        self.assertEqual((p["max_concurrent"], p["max_same_direction"]), ({"value": 2}, {"value": 2}))
        self.assertAlmostEqual(p["avg_concurrent"]["value"], 1.0)             # (2*1 + 2*2 + 2*1 + 2*0 + 1*1) / 9
        share = p["concurrency_share"]
        self.assertAlmostEqual(share["0"], 2 / 9)
        self.assertAlmostEqual(share["1"], 5 / 9)
        self.assertAlmostEqual(share["2"], 2 / 9)
        self.assertEqual(p["peak_initial_risk"], {"value": 200.0})
        self.assertAlmostEqual(p["peak_notional"]["value"], 2 * 100.0 * 100.0)   # qty 100 at 100 each
        self.assertEqual(p["leverage"]["reason"], "source_not_available")
        self.assertEqual(engine.portfolio([])["max_concurrent"]["reason"], "insufficient_data")

    def test_periods_by_hand(self):
        from datetime import timedelta
        from app.analytics import engine
        from tests.analytics_data import T0
        day = timedelta(days=1)
        # exits: day 0 (+50, -30), day 2 (+20); day 1 empty; equity 10000 at day 0 start, 10020 at day 2 start
        ts = [trade(1, 50.0), trade(2, -30.0), trade(3, 20.0, entry_bar=2 * 96 + 10)]
        eq = [(T0, 10000.0), (T0 + 2 * day, 10020.0)]
        rows = engine.periods(ts, eq, "day", 10000.0)
        self.assertEqual([r["period"] for r in rows], ["2026-03-02", "2026-03-03", "2026-03-04"])
        self.assertEqual([r["trades"]["value"] for r in rows], [2, 0, 1])
        self.assertAlmostEqual(rows[0]["return"]["value"], 20 / 10000)
        self.assertEqual(rows[1]["net_pnl"]["reason"], "insufficient_data")
        self.assertAlmostEqual(rows[2]["return"]["value"], 20 / 10020)
        self.assertEqual(engine.periods(ts, eq, "week", 10000.0)[0]["period"], "2026-03-02")   # Monday
        self.assertEqual(engine.periods(ts, eq, "month", 10000.0)[0]["period"], "2026-03-01")
        with self.assertRaises(ValueError):
            engine.periods(ts, eq, "year", 10000.0)

    def test_live_regimes_and_setup_join(self):
        from app.analytics import engine
        from app.storage.regime import SQLiteRegimeSnapshotStore
        from app.storage.setup import SQLiteSetupStore
        from tests.setup_data import PATH_A, evaluate
        lab = AnalyticsLab([])
        try:
            snap = evaluate(PATH_A, 18)                              # live setups: breakout @14, pullback @16
            SQLiteSetupStore(lab.db.writer).save(snap)
            b = next(s for s in snap.setups if s.setup_type.value == "breakout")
            regs = SQLiteRegimeSnapshotStore(lab.db.writer)
            regs.save(regime_snapshot("range", b.setup_time - M15_))
            regs.save(regime_snapshot("up", b.setup_time))            # same bar as the breakout
            r = lab.reader()
            rows = r.regime_snapshots()
            lr = engine.live_regimes(rows)
            self.assertEqual((lr["snapshots"], lr["regime_changes"]), ({"value": 2}, {"value": 1}))
            self.assertEqual(lr["regime"], {"ranging": 1, "trending_up": 1})
            j = engine.live_setups_by_regime(r.live_setups(), rows)
            self.assertEqual(j, {"trending_up": 1, "unknown_no_snapshot": 1})    # pullback bar has no snapshot
            r.close()
        finally:
            lab.close()


from datetime import timedelta as _td  # noqa: E402
M15_ = _td(minutes=15)


def outcome(setup_id, horizon, status="valid", **values):
    from app.domain.enums import OutcomeStatus
    from app.domain.research_lab import OutcomeObservation
    from tests.analytics_data import T0
    return OutcomeObservation(setup_id, "ETHUSDT", "breakout", "bullish", "confirmed", T0, horizon,
                              OutcomeStatus(status), "ok" if status == "valid" else "END_OF_DATA:1_of_5_bars",
                              values if status == "valid" else {})


class FunnelUntradedTests(unittest.TestCase):
    """Step 8 (TZ 5.7, 5.9). Research funnel: 10 observed; skips type_not_in_rule 2, not_confirmed 3
    -> 5 passed; execution skips no_next_bar 1, position_already_open 1 -> 3 entered; 2 profitable.
    conversions 5/10, 3/5, 2/3."""

    def test_research_funnel_by_hand(self):
        from app.analytics import engine
        obs = [{"setup": None}] * 10
        summ = {"skipped_signals": {"type_not_in_rule": 2, "not_confirmed": 3, "no_next_bar": 1,
                                    "position_already_open": 1}}
        f = engine.research_funnel(obs, summ, [trade(1, 5.0), trade(2, 3.0), trade(3, -1.0)])
        self.assertEqual([s["count"]["value"] for s in f["steps"]], [10, 5, 3, 2])
        self.assertEqual([s["conversion"]["value"] for s in f["steps"][1:]], [0.5, 0.6, 2 / 3])
        self.assertEqual(f["steps"][1]["rejection"], {"value": 0.5})
        self.assertEqual(f["warnings"], [])
        self.assertEqual(f["not_recorded_stages"]["ai_approved"]["reason"], "source_not_available")
        bad = engine.research_funnel(obs, summ, [trade(1, 5.0)])
        self.assertIn("FUNNEL_INCONSISTENT: skips imply 3 entries, sim_trades holds 1", bad["warnings"])
        worse = engine.research_funnel(obs[:1], {"skipped_signals": {}}, [trade(1, 5.0), trade(2, 1.0)])
        self.assertEqual(worse["steps"][2]["conversion"],                     # 2 entered from 1 observed
                         {"status": "not_available", "reason": "count_exceeds_previous_step"})
        self.assertTrue(worse["warnings"][0].startswith("FUNNEL_INCONSISTENT"))

    def test_live_funnel_by_hand(self):
        from app.analytics import engine
        from tests.analytics_data import M15, T0
        S = lambda i, t="breakout": {"setup_id": i, "setup_type": t, "direction": "bullish", "timeframe": "15m"}  # noqa
        E = lambda i, st, k: {"setup_id": i, "status": st, "at": T0 + M15 * k}  # noqa: E731
        setups = [S("a"), S("b"), S("c"), S("d", "pullback")]
        events = [E("a", "candidate", 0), E("a", "confirmed", 2), E("a", "invalidated", 3),
                  E("b", "candidate", 0), E("b", "expired", 12), E("c", "candidate", 5), E("c", "confirmed", 9),
                  E("d", "candidate", 1)]
        f = engine.live_setup_funnel(setups, events)
        t = f["total"]
        self.assertEqual([t[k]["value"] for k in ("candidates", "confirmed", "invalidated", "expired", "still_active")],
                         [4, 2, 1, 1, 2])
        self.assertEqual(t["confirmation_rate"], {"value": 0.5})
        self.assertEqual(t["median_time_to_confirm_s"], {"value": 2700.0})       # (30m + 60m) / 2
        self.assertEqual(f["groups"]["pullback/bullish/15m"]["confirmed"], {"value": 0})

    def test_untraded_post_setup_analysis(self):
        from app.analytics import engine
        from app.domain.market import Timeframe
        outs = [outcome("su_1", 5, forward_return=0.01, mfe=0.02, mae=0.01, first_1atr="favorable"),   # traded
                outcome("su_9", 5, forward_return=-0.02, mfe=0.01, mae=0.03, first_1atr="adverse"),
                outcome("su_9", 10, status="censored")]
        u = engine.untraded(outs, [trade(1, 5.0)], Timeframe.M15)
        self.assertEqual((u["label"], u["untraded_setups"]), ("POST_SETUP_ANALYSIS", 1))
        h5 = u["by_horizon"]["5"]
        self.assertEqual((h5["horizon_minutes"], h5["median_forward_return"]), (75.0, {"value": -0.02}))
        self.assertEqual(h5["first_1atr"]["adverse"], 1)
        self.assertEqual(h5["hypothetical_r"]["reason"], "no_stop_defined")
        self.assertEqual(u["by_horizon"]["10"]["counts"]["censored"], 1)
        self.assertEqual(u["by_horizon"]["10"]["median_mfe"]["status"], "not_available")


class FunnelOnRealBacktestTests(unittest.TestCase):
    """Step 8 check against a REAL Phase 7 replay + backtest: the funnel reconstructed from
    skipped_signals must agree with sim_trades (verifies the skip semantics assumption)."""

    def test_real_run_is_consistent(self):
        from app.analytics import engine
        from app.storage.analytics_read import SQLiteAnalyticsReader
        from tests.research_data import Lab, bcfg
        lab = Lab()
        try:
            rp = lab.replay()
            res, _, _ = lab.pipe.backtest(rp.run_id, bcfg())
            r = SQLiteAnalyticsReader(lab.db.path)
            f = engine.research_funnel(r.observations(rp.run_id), r.run(res.run_id)["summary"], r.trades(res.run_id))
            r.close()
            self.assertEqual(f["warnings"], [])
            self.assertEqual([s["count"]["value"] for s in f["steps"]][:3], [3, 1, 1])   # 3 setups, 1 breakout traded
        finally:
            lab.close()


class VersionCompareTests(unittest.TestCase):
    """Step 9 (TZ section 3 and 5.12)."""

    def test_stale_versions_excluded_by_default(self):
        from app.analytics import engine
        from app.research.lab.versions import component_versions
        cur = component_versions()
        run = {"run_id": "bt_old", "versions": {**cur, "setup": "0:old"}}
        self.assertEqual(engine.run_version_status(run, cur), "version_mismatch")
        self.assertEqual(engine.guard_run(run, cur, False), ({"status": "not_available", "reason": "version_mismatch"},
                                                             []))
        na, warns = engine.guard_run(run, cur, True)
        self.assertIsNone(na)
        self.assertTrue(warns[0].startswith("VERSION_MISMATCH: run bt_old"))
        self.assertEqual(engine.guard_run({"run_id": "x", "versions": cur}, cur, False), (None, []))

    def test_mixed_configs(self):
        from app.analytics import engine
        rows = [{"engine_version": "1", "config_hash": "a"}, {"engine_version": "1", "config_hash": "b"}]
        self.assertEqual(engine.guard_mixed(rows, ("engine_version", "config_hash"), False)[0]["reason"],
                         "mixed_configs")
        self.assertTrue(engine.guard_mixed(rows, ("engine_version", "config_hash"), True)[1][0].startswith(
            "MIXED_CONFIGS: 2"))
        self.assertEqual(engine.guard_mixed(rows[:1], ("config_hash",), False), (None, []))

    def test_compare(self):
        from app.analytics import engine
        from app.research.lab.versions import component_versions
        v = component_versions()
        a = {"run_id": "bt_a", "dataset_id": "ds", "versions": v, "config": {"rule_name": "r1", "initial_equity_quote": 1e4}}
        b = {**a, "run_id": "bt_b", "config": {"rule_name": "r2", "initial_equity_quote": 1e4}}
        tr = {"bt_a": [trade(1, 10.0)], "bt_b": [trade(1, -4.0, run_id="bt_b")]}
        c = engine.compare_runs([a, b], tr, {})
        self.assertTrue(c["comparable"])
        self.assertEqual((c["runs"]["bt_a"]["net_pnl"], c["runs"]["bt_b"]["net_pnl"]), ({"value": 10.0},
                                                                                         {"value": -4.0}))
        self.assertIn("no run is selected as better", c["note"])
        other = {**b, "dataset_id": "ds2"}
        c2 = engine.compare_runs([a, other], tr, {})
        self.assertEqual((c2["comparable"], c2["reasons"], c2["runs"]), (False, ["different_datasets"], {}))
        c3 = engine.compare_runs([a, other], tr, {}, force=True)
        self.assertIn("NOT COMPARABLE", c3["warning"])
        with self.assertRaises(ValueError):
            engine.compare_runs([a], tr, {})


class DataQualityMonitorTests(unittest.TestCase):
    """Step 10 (TZ 5.13-5.14): each check catches its own corrupted record; a clean DB has no critical finding."""

    def findings(self, lab):
        from app.analytics import quality
        from app.research.lab.versions import component_versions
        r = lab.reader()
        try:
            return quality.data_quality(r, component_versions())
        finally:
            r.close()

    def test_clean_database_has_no_critical_finding(self):
        lab = AnalyticsLab([trade(1, 50.0), trade(2, -30.0)])
        try:
            dq = self.findings(lab)
            self.assertEqual(dq["counts"]["critical"], 0)
            self.assertEqual(dq["summary"]["sim_trades"]["valid"], 2)
            probs = {(f["table"], f["problem"][:40]) for f in dq["findings"]}
            # the only expected warnings: no candles stored for the fixture windows, conflict tables lack triggers (D3)
            self.assertIn(("research_conflicts", "no append-only triggers on this audit ta"), probs)
            self.assertEqual({f["severity"] for f in dq["findings"]} - {"warning", "info"}, set())
        finally:
            lab.close()

    def test_every_check_catches_its_corruption(self):
        import json
        from app.research.lab.versions import component_versions
        lab = AnalyticsLab([trade(1, 50.0), trade(2, 10.0, setup_id="su_1"), trade(3, 5.0, risk=0.0)])
        try:
            lab.add_backtest("bt_old", [trade(1, 1.0, run_id="bt_old")], versions={**component_versions(),
                                                                                   "setup": "0:old"})
            c = lab.db.conn
            good = trade(4, 7.0).to_dict()
            bad = {**good, "trade_no": 4, "exit_time": good["decision_time"], "entry_time": good["exit_time"],
                   "net_pnl": 999.0}
            c.execute("INSERT INTO sim_trades VALUES (?,?,?,?,?,?,?,?,?)",
                      (RUN, 4, "ds_fixture", "ETHUSDT", "su_4", 0, 0, 999.0, json.dumps(bad)))
            c.execute("PRAGMA foreign_keys = OFF")
            c.execute("INSERT INTO sim_trades VALUES (?,?,?,?,?,?,?,?,?)",
                      ("bt_ghost", 1, "ds_fixture", "ETHUSDT", "su_x", 0, 0, 7.0, json.dumps({**good, "run_id": "bt_ghost"})))
            c.execute("INSERT INTO setup_events VALUES ('e1','su_missing','candidate',1,'x',1,'t')")
            c.execute("PRAGMA foreign_keys = ON")
            c.execute("INSERT INTO research_runs VALUES ('oc_x','outcomes','rp_fixture','ds_fixture','{}',?, '{}','h','t')",
                      (json.dumps(component_versions()),))
            c.execute("INSERT INTO outcome_observations VALUES ('oc_x','su_1',5,'valid',?)",
                      (json.dumps({"values": {}}),))
            c.execute("INSERT INTO setups VALUES ('su_live','linear','ETHUSDT','15m','breakout','bullish','t',1,1.0,"
                      "'1','h',1,'{}','t')")
            c.execute("INSERT INTO research_conflicts(run_id, stored_hash, new_hash, created_at) VALUES ('r','a','b','t')")
            c.execute("INSERT INTO monitor_runs(run_id, started_at, status, config_json, meta_json, last_heartbeat_at) "
                      "VALUES ('m1','2026-03-01T00:00:00+00:00','running','{}','{}','2026-03-01T01:00:00+00:00')")
            c.execute("INSERT INTO monitor_runs(run_id, started_at, status, config_json, meta_json) "
                      "VALUES ('m2','2026-03-02T00:00:00+00:00','abandoned','{}','{}')")
            dq = self.findings(lab)
            got = {(f["table"], f["record"], f["problem"], f["severity"]) for f in dq["findings"]}
            expect = {
                ("sim_trades", f"{RUN}#4", "exit_time < entry_time", "critical"),
                ("sim_trades", f"{RUN}#4", "net != gross - fees + funding", "critical"),
                ("sim_trades", "bt_ghost#1", "orphan: run missing", "critical"),
                ("sim_trades", f"{RUN}#3", "initial_risk <= 0 (R undefined)", "warning"),
                ("sim_trades", f"{RUN}/su_1", "2 trades for one setup in a run", "warning"),
                ("outcome_observations", "oc_x/su_1/h5", "valid without values", "critical"),
                ("setup_events", "su_missing:candidate", "orphan: setup missing", "critical"),
                ("setups", "su_live", "no candidate event", "critical"),
                ("research_conflicts", "1", "recorded conflict (a later computation disagreed)", "warning"),
                ("monitor_runs", "m1", "status running, but a newer run started after its last heartbeat (stale, "
                                       "not closed)", "warning"),
                ("monitor_runs", "m2", "run abandoned", "warning"),
                ("research_runs", "bt_old", "version_mismatch with current component versions", "info"),
            }
            self.assertEqual(expect - got, set())
            self.assertGreaterEqual(dq["counts"]["critical"], 6)
            self.assertEqual(dq["summary"]["sim_trades"]["orphaned"], 1)
            self.assertEqual(dq["note"], "diagnostic only: nothing was corrected or written")
        finally:
            lab.close()

    def test_monitor_summary(self):
        from app.analytics import quality
        lab = AnalyticsLab([])
        try:
            c = lab.db.conn
            c.execute("INSERT INTO monitor_runs(run_id, started_at, status, config_json, meta_json, summary_json) "
                      "VALUES ('m1','2026-03-01T00:00:00+00:00','completed','{}','{}','{\"cycles_with_errors\": 2}')")
            c.execute("INSERT INTO monitor_events(run_id, cycle_id, symbol, timeframe, as_of_ms, status, persisted, "
                      "duration_ms, recorded_at) VALUES ('m1','c1','ETHUSDT','15m',1,'ok','saved',10,'t')")
            c.execute("INSERT INTO monitor_events(run_id, cycle_id, symbol, timeframe, as_of_ms, status, persisted, "
                      "duration_ms, recorded_at) VALUES ('m1','c2','ETHUSDT','15m',2,'persistence_error','failed',30,'t')")
            r = lab.reader()
            m = quality.monitor_summary(r)
            r.close()
            self.assertEqual((m["runs"], m["runs_by_status"], m["cycles_with_errors"]),
                             ({"value": 1}, {"completed": 1}, {"value": 2}))
            self.assertEqual(m["persist_failed_share"], {"value": 0.5})
            self.assertEqual(m["median_duration_ms"], {"value": 20.0})
        finally:
            lab.close()


class AnalyticsSmokeTests(unittest.TestCase):
    """Light component check for the runner registry (the full suite runs as categories)."""

    def test_engine_summary_on_fixture(self):
        from app.analytics.engine import AnalyticsEngine
        from app.research.lab.versions import component_versions
        lab = AnalyticsLab([trade(1, 10.0)])
        try:
            r = lab.reader()
            env = AnalyticsEngine(r, component_versions()).summary()
            r.close()
            self.assertEqual(env["data"]["research_runs"][-1]["version_status"], "current")
            self.assertEqual(env["data"]["real_trades_rows"], 0)
        finally:
            lab.close()
