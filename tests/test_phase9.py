"""Phase 9 Strategy + Risk Engine. Expected values are derived BY HAND in docstrings.
PATH_A (tests/setup_data.py, W=200): breakout setup - candidate @14, confirmed @16 (close 111),
invalidated @17; level 108, ATR at trigger ~3.57. PATH_B bar 9: bearish sweep reversal and
bullish range rejection are both born on bar 9 (conflicting)."""
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

from app.domain.decision import (ConflictRef, GateResult, InstrumentRules, OpenExposure, PortfolioState, RiskCheck,
                                 RiskDecision, RiskPlan, SetupView, StrategyDecision, setup_view)
from app.domain.enums import PositionSide, RiskStatus, SetupStatus
from app.domain.errors import DomainError
from tests.setup_data import PATH_A, PATH_B, W, by_type, evaluate
from tests.structure_data import bar_close


def close(i):
    return bar_close(i, warmup=W)


class ContractTests(unittest.TestCase):
    """Step 2: contracts and the point-in-time SetupView."""

    @classmethod
    def setUpClass(cls):
        cls.a = evaluate(PATH_A, 18)
        cls.b = evaluate(PATH_B, 9)

    def breakout(self):
        return by_type(self.a, "breakout")[0]

    def test_view_is_point_in_time(self):
        s = self.breakout()
        v16 = setup_view(s, close(16), "allowed")
        self.assertEqual((v16.status_at_decision, v16.confirmed_at, v16.setup_time),
                         (SetupStatus.CONFIRMED, close(16), close(14)))
        v15 = setup_view(s, close(15), "unknown")
        self.assertEqual((v15.status_at_decision, v15.confirmed_at), (SetupStatus.CANDIDATE, None))
        self.assertEqual(setup_view(s, close(17), "allowed").status_at_decision, SetupStatus.INVALIDATED)
        with self.assertRaises(DomainError):
            setup_view(s, close(13), "allowed")                    # not born yet
        self.assertFalse(hasattr(v16, "contradictions"))           # computed at last_seen -> never exposed
        self.assertEqual(v16.evidence_map["level"], 108.0)

    def test_conflicts_only_if_active_at_decision(self):
        sweep, rng = by_type(self.b, "sweep_reversal")[0], by_type(self.b, "range_rejection")[0]
        v = setup_view(sweep, close(9), "unknown", self.b.setups)
        self.assertEqual(v.conflicting_active, (ConflictRef(rng.setup_id, "range_rejection", "bullish"),))
        a_setups = self.a.setups                                  # breakout + pullback, both bullish -> no conflict
        self.assertEqual(setup_view(self.breakout(), close(16), "allowed", a_setups).conflicting_active, ())

    def test_view_invariants(self):
        v = setup_view(self.breakout(), close(16), "allowed")
        for bad in (dict(direction="unknown"), dict(setup_time=close(17)), dict(confirmed_at=close(17)),
                    dict(first_regime_fit="maybe")):
            with self.subTest(bad=bad), self.assertRaises(DomainError):
                replace(v, **bad)

    def test_strategy_decision_invariants(self):
        ok = StrategyDecision("sd1", "su", "ETHUSDT", close(16), True, PositionSide.LONG, "strategy:accepted", "",
                              "s", "1", "h", D("107"), D("2"), 20)
        self.assertEqual(ok.stop_reference, D("107"))
        for bad in (dict(reason_code="strategy:vibes"), dict(accepted=False),
                    dict(stop_reference=None), dict(direction=None)):
            with self.subTest(bad=bad), self.assertRaises(DomainError):
                replace(ok, **bad)
        no = StrategyDecision("sd2", "su", "ETHUSDT", close(16), False, None, "strategy:unsupported_setup", "range",
                              "s", "1", "h")
        with self.assertRaises(DomainError):
            replace(no, direction=PositionSide.LONG)

    def test_risk_decision_and_gate_invariants(self):
        plan = RiskPlan("rp", "su", PositionSide.LONG, D("109"), D("108"), D("100"))
        ok = RiskDecision("rd", "su", RiskStatus.APPROVED, "risk:approved", ("risk:approved",), plan, D("100"),
                          D("100"), D("10900"), (RiskCheck("stop_side", True, "108", "<109"),), "equity_pct", "1", "h")
        with self.assertRaises(DomainError):
            replace(ok, risk_plan=None)
        with self.assertRaises(DomainError):
            replace(ok, status=RiskStatus.REJECTED)                 # rejected cannot carry a plan
        with self.assertRaises(DomainError):
            replace(ok, reason_code="stop_on_wrong_side")           # namespace required
        with self.assertRaises(DomainError):
            replace(ok, mode="kelly")
        sd = StrategyDecision("sd1", "su", "ETHUSDT", close(16), True, PositionSide.LONG, "strategy:accepted", "",
                              "s", "1", "h", D("108"), D("2"), 20)
        self.assertTrue(GateResult("su", sd, ok, True, "risk:approved").enter)
        with self.assertRaises(DomainError):
            GateResult("su", sd, ok, False, "risk:approved")
        rej = replace(sd, accepted=False, direction=None, reason_code="strategy:invalid_setup", stop_reference=None,
                      take_profit_r=None, max_hold_bars=None)
        with self.assertRaises(DomainError):
            GateResult("su", rej, ok, True, "x")                    # risk only for accepted strategy decisions

    def test_portfolio_and_instrument(self):
        from app.domain.market import InstrumentInfo, Symbol
        p = PortfolioState(close(16), D("9000"), D("10000"), D("9500"), D("-500"),
                           (OpenExposure("BTCUSDT", PositionSide.LONG, D("50"), D("5000")),
                            OpenExposure("SOLUSDT", PositionSide.SHORT, D("30"), D("900"))))
        self.assertEqual(p.open_risk, D("80"))
        with self.assertRaises(DomainError):
            PortfolioState(close(16), D("11000"), D("10000"), D("1"), D("0"))
        i = InstrumentInfo(Symbol("ETHUSDT"), D("0.01"), D("0.01"), D("0.01"), D("5"), max_leverage=D("100"))
        r = InstrumentRules.from_instrument(i)
        self.assertEqual((r.qty_step, r.min_qty, r.min_notional, r.max_leverage), (D("0.01"), D("0.01"), D("5"),
                                                                                    D("100")))

    def test_legacy_classes_kept_and_marked(self):
        import app.domain.decision as d
        self.assertIn("DEPRECATED", d.Setup.__doc__)
        self.assertIn("DEPRECATED", d.Signal.__doc__)
        self.assertIsNotNone(d.EntryPlan)


def strat(**ch):
    from app.strategy.config import StrategyConfig
    from app.strategy.engine import StrategyV1
    from tests.structure_data import ROOT
    c = StrategyConfig.from_file(ROOT / "config" / "strategy_v1.json")
    return StrategyV1(c.with_(**ch) if ch else c)


class StrategyTests(unittest.TestCase):
    """Step 3. PATH_A breakout confirmed at close 16: stop = 108 - 0.25 * 3.573164394701848
    = 107.106708901324538; mirrored short: 92 + 0.893291098675462 = 92.893291098675462."""

    @classmethod
    def setUpClass(cls):
        from tests.structure_data import mirror
        cls.a = evaluate(PATH_A, 18)
        cls.m = evaluate(mirror(PATH_A), 18)
        cls.b = evaluate(PATH_B, 10)

    def view(self, snap, typ, i, fit="allowed", others=()):
        return setup_view(by_type(snap, typ)[0], close(i), fit, others)

    def test_accepted_long_and_short_by_hand(self):
        d = strat().evaluate(self.view(self.a, "breakout", 16))
        self.assertEqual((d.accepted, d.direction, d.reason_code), (True, PositionSide.LONG, "strategy:accepted"))
        self.assertEqual(d.stop_reference, D("107.106708901324538"))
        self.assertEqual((d.take_profit_r, d.max_hold_bars), (D("2.0"), 20))
        s = strat().evaluate(self.view(self.m, "breakout", 16))
        self.assertEqual((s.direction, s.stop_reference), (PositionSide.SHORT, D("92.893291098675462")))

    def test_rejections(self):
        cases = [
            (strat(setup_types=["breakout"]), self.view(self.a, "pullback", 16), "strategy:unsupported_setup",
             "type_not_in_strategy:pullback"),
            (strat(), self.view(self.a, "breakout", 15), "strategy:invalid_setup", "not_confirmed_at_decision"),
            (strat(), self.view(self.a, "breakout", 17), "strategy:invalid_setup", "setup_closed:invalidated"),
            (strat(), self.view(self.a, "breakout", 16, "not_allowed"), "strategy:invalid_setup",
             "regime_fit_not_allowed:not_allowed"),
        ]
        for s, v, code, detail in cases:
            with self.subTest(detail=detail):
                d = s.evaluate(v)
                self.assertEqual((d.accepted, d.direction, d.reason_code, d.reason_detail), (False, None, code, detail))

    def test_missing_context(self):
        v = self.view(self.a, "breakout", 16)
        no_atr = replace(v, evidence=tuple((k, x) for k, x in v.evidence if k != "atr"))
        self.assertEqual(strat().evaluate(no_atr).reason_code, "strategy:missing_required_context")
        sweep = self.view(self.b, "sweep_reversal", 9, "unknown")
        no_ext = replace(sweep, evidence=tuple((k, x) for k, x in sweep.evidence if k != "sweep_extreme"))
        d = strat(entry_on="candidate").evaluate(no_ext)
        self.assertEqual((d.reason_code, d.reason_detail), ("strategy:missing_required_context", "no_invalidation_ref"))

    def test_conflicting_setups(self):
        v = self.view(self.b, "sweep_reversal", 9, "unknown", self.b.setups)     # range rejection (bullish) active
        d = strat(entry_on="candidate").evaluate(v)
        self.assertEqual((d.reason_code, d.reason_detail), ("strategy:conflicting_setup", "range_rejection:bullish"))
        ok = strat(entry_on="candidate", reject_conflicting_setups=False).evaluate(v)
        self.assertEqual((ok.accepted, ok.direction), (True, PositionSide.SHORT))
        self.assertEqual(ok.stop_reference, D("106.5") + D("0.25") * D(str(dict(v.evidence)["atr"])))

    def test_deterministic_and_config(self):
        from app.domain.errors import DomainError as DE
        from app.strategy.config import StrategyConfig
        v = self.view(self.a, "breakout", 16)
        self.assertEqual(strat().evaluate(v), strat().evaluate(v))
        self.assertNotEqual(strat().evaluate(v).decision_id, strat(take_profit_r=3).evaluate(v).decision_id)
        base = strat().cfg.canonical()
        for k in base:
            if k != "schema":
                with self.subTest(missing=k), self.assertRaises(DE):
                    StrategyConfig.from_mapping({x: y for x, y in base.items() if x != k})
        for bad in (dict(entry_on="always"), dict(setup_types=["scalp"]), dict(regime_fit_allowed=[]),
                    dict(take_profit_r=0), dict(max_hold_bars=0), dict(reject_conflicting_setups="yes")):
            with self.subTest(bad=bad), self.assertRaises(DE):
                StrategyConfig.from_mapping({**base, **bad})

    def test_version_lock(self):
        from app.strategy.version import locked, rules_hash
        self.assertEqual(rules_hash(), locked()["rules_hash"])


def sd(side=PositionSide.LONG, stop="99"):
    return StrategyDecision("sd_x", "su_x", "ETHUSDT", close(16), True, side, "strategy:accepted", "", "s", "1", "h",
                            D(stop), D("2"), 20)


def pf(equity="10000", peak=None, day_start=None, today="0", open_=(), **kw):
    return PortfolioState(close(16), D(equity), D(peak or equity), D(day_start or equity), D(today), tuple(open_), **kw)


def rules(step="0.01", min_qty="0.01", min_notional="5", lev=None):
    return InstrumentRules("ETHUSDT", D(step), D(min_qty), None if min_notional is None else D(min_notional),
                           None if lev is None else D(lev))


def risk(mode="equity_pct", quote=None):
    from app.config.settings import RiskSettings
    from app.risk.config import RiskConfig
    from app.risk.engine import RiskEngineV1
    return RiskEngineV1(RiskConfig.from_settings(RiskSettings(), mode, quote))


class RiskEngineTests(unittest.TestCase):
    """Step 4. Defaults (RiskSettings): risk 0.5 %, open risk 2.5 %, leverage 3, 3 positions,
    daily 2 %, kill switch 15 %, hard drawdown 20 %. Equity 10000 -> budget 50."""

    def test_normal_sizing_by_hand(self):
        r = risk().assess(sd(stop="99"), D("100"), pf(), rules())
        self.assertEqual((r.status, r.reason_code, r.qty, r.risk_amount, r.notional),
                         (RiskStatus.APPROVED, "risk:approved", D("50.00"), D("50.00"), D("5000.00")))
        self.assertEqual((r.risk_plan.planned_entry, r.risk_plan.initial_stop, r.risk_plan.planned_qty),
                         (D("100"), D("99"), D("50.00")))
        self.assertEqual([c.name for c in r.checks], ["hard_drawdown", "kill_switch", "daily_loss", "open_positions",
                                                      "stop_side", "instrument_rules", "size", "leverage", "minimums",
                                                      "open_risk"])

    def test_rounding_down_to_step(self):
        r = risk().assess(sd(stop="99.3"), D("100"), pf(), rules())      # 50 / 0.7 = 71.428... -> 71.42
        self.assertEqual(r.qty, D("71.42"))

    def test_leverage_reduces_then_minimums_rechecked(self):
        r = risk().assess(sd(stop="99.9"), D("100"), pf(), rules())      # 500 * 100 = 50000 > 3 * 10000
        self.assertEqual((r.status, r.qty, r.reason_codes), (RiskStatus.APPROVED, D("300.00"),
                                                             ("risk:reduced_leverage_cap", "risk:approved")))
        r2 = risk().assess(sd(stop="99.9"), D("100"), pf(), rules(lev="2"))   # instrument cap 20000 -> 200
        self.assertEqual(r2.qty, D("200.00"))
        # min notional 25000: before reduction 50000 would pass; after reduction 20000 fails -> reject
        r3 = risk().assess(sd(stop="99.9"), D("100"), pf(), rules(lev="2", min_notional="25000"))
        self.assertEqual((r3.status, r3.reason_code, r3.risk_plan), (RiskStatus.REJECTED, "risk:below_min_size", None))
        self.assertIn("risk:reduced_leverage_cap", r3.reason_codes)

    def test_minimums_never_inflate(self):
        r = risk().assess(sd(stop="90"), D("100"), pf(equity="1"), rules(step="0.001", min_qty="0.01", min_notional=None))
        # budget 0.005 / 10 = 0.0005 -> floor 0.000 -> below minimum; never raised to 0.01
        self.assertEqual((r.status, r.reason_code, r.qty), (RiskStatus.REJECTED, "risk:below_min_size", D("0")))

    def test_open_risk_limit_rejects_without_reduction(self):
        open_ = (OpenExposure("BTCUSDT", PositionSide.LONG, D("220"), D("1000")),)
        r = risk().assess(sd(), D("100"), pf(open_=open_), rules())        # 220 + 50 > 250
        self.assertEqual((r.status, r.reason_code, r.qty), (RiskStatus.REJECTED, "risk:open_risk_limit", D("0")))
        chk = {c.name: c for c in r.checks}["open_risk"]                  # the evaluated size stays visible
        self.assertEqual((chk.passed, chk.value, chk.limit), (False, "270.00", "<=250.000"))
        ok = risk().assess(sd(), D("100"), pf(open_=(OpenExposure("BTCUSDT", PositionSide.LONG, D("200"), D("1")),)),
                           rules())                                        # 200 + 50 = 250 -> allowed
        self.assertEqual(ok.status, RiskStatus.APPROVED)

    def test_global_stops(self):
        E = OpenExposure("X", PositionSide.LONG, D("1"), D("1"))
        cases = [(pf(open_=(E, E, E)), RiskStatus.REJECTED, "risk:max_open_positions"),
                 (pf(today="-200"), RiskStatus.REJECTED, "risk:daily_loss_limit"),          # 2 % of 10000
                 (pf(today="-199.99"), RiskStatus.APPROVED, "risk:approved"),
                 (pf(equity="8500", peak="10000"), RiskStatus.REJECTED, "risk:kill_switch_active"),   # 15 %
                 (pf(equity="8000", peak="10000"), RiskStatus.HALTED, "risk:hard_drawdown_halt"),     # 20 %
                 (pf(kill_switch=True), RiskStatus.REJECTED, "risk:kill_switch_active"),   # latched
                 (pf(halted=True), RiskStatus.HALTED, "risk:hard_drawdown_halt")]          # latched
        for p, status, code in cases:
            with self.subTest(code=code, p=p):
                r = risk().assess(sd(), D("100"), p, rules())
                self.assertEqual((r.status, r.reason_code), (status, code))
                if status is not RiskStatus.APPROVED:
                    self.assertIsNone(r.risk_plan)                          # no plan -> nothing to enter, nothing closed

    def test_stop_side_both_modes(self):
        for mode, q in (("equity_pct", None), ("fixed_quote", "100")):
            for side, stop, ok in ((PositionSide.LONG, "99.99", True), (PositionSide.LONG, "100", False),
                                   (PositionSide.SHORT, "100.01", True), (PositionSide.SHORT, "100", False)):
                with self.subTest(mode=mode, side=side, stop=stop):
                    r = risk(mode, q).assess(sd(side, stop), D("100"), pf(), rules())
                    self.assertEqual(r.reason_code == "risk:stop_on_wrong_side", not ok)

    def test_instrument_rules_required_in_equity_pct(self):
        r = risk().assess(sd(), D("100"), pf(), None)
        self.assertEqual((r.status, r.reason_code), (RiskStatus.REJECTED, "risk:instrument_rules_unavailable"))

    def test_fixed_quote_ignores_instrument_rules_and_portfolio(self):
        """D9-3: 100 / |109 - 108| = 100 whatever the rules; rules only recorded as diagnostics."""
        base = risk("fixed_quote", "100").assess(sd(stop="108"), D("109"), pf(), None)
        self.assertEqual((base.status, base.qty), (RiskStatus.APPROVED, D("100")))
        for rr in (rules(), rules(step="7", min_qty="1000", min_notional="1e9", lev="1")):
            r = risk("fixed_quote", "100").assess(sd(stop="108"), D("109"), pf(equity="8000", peak="10000",
                                                                                 halted=True), rr)
            self.assertEqual((r.status, r.qty, r.risk_plan.planned_qty), (RiskStatus.APPROVED, D("100"), D("100")))
            self.assertEqual(dict(r.instrument_diagnostic)["qty_step"], str(rr.qty_step))

    def test_deterministic_and_config(self):
        from app.domain.errors import DomainError as DE
        from app.risk.config import RiskConfig
        from app.config.settings import RiskSettings
        a = risk().assess(sd(), D("100"), pf(), rules())
        self.assertEqual(a, risk().assess(sd(), D("100"), pf(), rules()))
        with self.assertRaises(ValueError):
            risk().assess(replace(sd(), accepted=False, direction=None, reason_code="strategy:invalid_setup",
                                  stop_reference=None, take_profit_r=None, max_hold_bars=None), D("100"), pf(), rules())
        for bad in (dict(mode="kelly"), dict(mode="fixed_quote")):
            with self.subTest(bad=bad), self.assertRaises(DE):
                RiskConfig.from_settings(RiskSettings(), **bad)
        self.assertNotEqual(RiskConfig.from_settings(RiskSettings()).config_hash,
                            RiskConfig.from_settings(RiskSettings(), "fixed_quote", 100).config_hash)

    def test_version_lock(self):
        from app.risk.version import locked, rules_hash
        self.assertEqual(rules_hash(), locked()["rules_hash"])


class RecordingGate:
    """Test double of the DecisionGate: approves everything with a fixed-quote plan and
    records the portfolio state it was given (the state under test)."""
    pipeline_id = "recording"
    entry_on = "confirmed"

    def __init__(self, halt_first=False, tp_r=2.0):
        self.config = {"gate": "recording", "halt_first": halt_first, "tp_r": tp_r}
        self.seen, self.halt_first, self.tp_r = [], halt_first, tp_r

    def evaluate(self, view, ref, portfolio, instrument):
        from app.domain.decision import GateResult
        self.seen.append((view.setup_type, portfolio))
        s = strat(setup_types=["breakout", "pullback", "sweep_reversal", "range_rejection"],
                  stop_buffer_atr=0, take_profit_r=self.tp_r).evaluate(view)
        if not s.accepted:
            return GateResult(view.setup_id, s, None, False, s.reason_code)
        if self.halt_first and len(self.seen) == 1:
            r = RiskDecision("rd_h", view.setup_id, RiskStatus.HALTED, "risk:hard_drawdown_halt",
                             ("risk:hard_drawdown_halt",), None, D(0), D(0), D(0), (), "equity_pct", "1", "h")
            return GateResult(view.setup_id, s, r, False, r.reason_code)
        r = risk("fixed_quote", "100").assess(s, ref, portfolio, instrument)
        return GateResult(view.setup_id, s, r, r.status is RiskStatus.APPROVED, r.reason_code)


def gate_run(gate, path=None):
    from app.research.lab.backtest import DecisionBacktestEngine
    from app.research.lab.config import DecisionRunConfig
    from tests.research_data import M15, PATH_BT, Lab
    lab = Lab(path=path or PATH_BT)
    try:
        rp = lab.replay()
        cs = {"ETHUSDT": lab.cs}
        cfg = DecisionRunConfig.from_mapping({"schema": 1, "initial_equity_quote": 10000,
                                              "costs": {"taker_fee_bps": 5.5, "maker_fee_bps": 2.0, "slippage_bps": 0,
                                                        "funding_mode": "none", "assumed_funding_rate_8h": 0.0001}})
        return DecisionBacktestEngine(cfg).run_gate(rp, cs, M15, {}, gate, {}, cfg.config_hash), rp
    finally:
        lab.close()


class DecisionLoopTests(unittest.TestCase):
    """Step 5. PATH_BT: breakout confirmed @16 -> entry open 17 (109), TP 111 hit on bar 17 (net 191.785);
    pullback confirmed @17 -> decision at open 18 sees equity 10191.785, realized today 191.785,
    day-start equity 10000, no open position."""

    def test_portfolio_state_at_decisions_by_hand(self):
        g = RecordingGate()
        res, _ = gate_run(g)
        (t1, p1), (t2, p2) = g.seen
        self.assertEqual((t1, p1.equity, p1.peak_equity, p1.open_positions), ("breakout", D("10000"), D("10000"), ()))
        self.assertEqual(t2, "pullback")
        self.assertAlmostEqual(float(p2.equity), 10191.785, places=6)
        self.assertAlmostEqual(float(p2.realized_today), 191.785, places=6)
        self.assertEqual((p2.day_start_equity, p2.open_positions), (D("10000.0"), ()))
        self.assertEqual([d["outcome"] for d in res.decisions], ["entered", "entered"])
        self.assertEqual(res.skipped.get("not_decidable"), 1)             # 2nd breakout never confirmed

    def test_halt_latches_and_blocks_entries(self):
        g = RecordingGate(halt_first=True)
        res, _ = gate_run(g)
        self.assertEqual(res.decisions[0]["outcome"], "risk:hard_drawdown_halt")
        self.assertEqual(res.decisions[0]["risk"]["status"], "halted")
        self.assertTrue(g.seen[1][1].halted)                               # latched for every later decision
        self.assertEqual(res.halted_at.isoformat(), res.decisions[0]["decision_time"])

    def test_open_position_is_visible_and_not_closed_by_risk(self):
        """Two symbols, same path, target 50R: at the breakout decision BTC is decided first and
        entered; ETH's decision at the same instant must see that BTC position: LONG, initial risk
        (109 - 108) * 100 = 100, notional 109 * 100 = 10900."""
        from app.domain.market import Symbol
        from app.research.lab.backtest import DecisionBacktestEngine
        from app.research.lab.config import DecisionRunConfig
        from tests.research_data import M15, PATH_BT, Lab, candles
        both = candles(PATH_BT, symbol=Symbol("BTCUSDT")) + candles(PATH_BT)
        lab = Lab(cs=both)
        try:
            ds, _ = lab.pipe.build_dataset(["BTCUSDT", "ETHUSDT"], M15, lab.cs[0].open_time,
                                           max(c.close_time for c in lab.cs))
            rp, _ = lab.pipe.replay(ds.dataset_id)
            g = RecordingGate(tp_r=50.0)
            cfg = DecisionRunConfig.from_mapping({"schema": 1, "initial_equity_quote": 10000, "costs": {
                "taker_fee_bps": 0, "maker_fee_bps": 0, "slippage_bps": 0, "funding_mode": "none",
                "assumed_funding_rate_8h": 0.0001}})
            by = {"BTCUSDT": [c for c in both if c.symbol.name == "BTCUSDT"],
                  "ETHUSDT": [c for c in both if c.symbol.name == "ETHUSDT"]}
            res = DecisionBacktestEngine(cfg).run_gate(rp, by, M15, {}, g, {}, cfg.config_hash)
        finally:
            lab.close()
        first = [p for typ, p in g.seen if typ == "breakout"]
        self.assertEqual(len(first), 2)                                    # BTC then ETH, same instant
        self.assertEqual(first[0].open_positions, ())
        (exp,) = first[1].open_positions
        self.assertEqual((exp.symbol, exp.side, exp.initial_risk, exp.notional),
                         ("BTCUSDT", PositionSide.LONG, D("100"), D("10900")))


class CompatGate:
    """Strategy v1 configured as a Phase 7 BacktestConfig v1 rule + Risk fixed_quote."""

    def __init__(self, bt):
        from app.config.settings import RiskSettings
        from app.risk.config import RiskConfig
        from app.risk.engine import RiskEngineV1
        self.strategy = strat(setup_types=[t.value for t in bt.setup_types], entry_on=bt.entry_on,
                              regime_fit_allowed=[f.value for f in bt.regime_fit_allowed],
                              reject_conflicting_setups=False, stop_buffer_atr=bt.stop_buffer_atr,
                              take_profit_r=bt.take_profit_r, max_hold_bars=bt.max_hold_bars)
        self.risk = RiskEngineV1(RiskConfig.from_settings(RiskSettings(), "fixed_quote", bt.risk_per_trade_quote))
        self.entry_on, self.pipeline_id = bt.entry_on, "compat"
        self.config = {"strategy": self.strategy.cfg.canonical(), "risk": self.risk.cfg.canonical()}

    def evaluate(self, view, ref, portfolio, instrument):
        s = self.strategy.evaluate(view)
        if not s.accepted:
            return GateResult(view.setup_id, s, None, False, s.reason_code)
        r = self.risk.assess(s, ref, portfolio, instrument)
        return GateResult(view.setup_id, s, r, r.status is RiskStatus.APPROVED, r.reason_code)


class EquivalenceTests(unittest.TestCase):
    """Step 6 / criterion E: Strategy v1 + Risk fixed_quote reproduces the Phase 7 v1 backtest trade for
    trade (all SimulatedTrade fields except run_id) and the equity curve, on several paths."""

    def compare(self, path, bt):
        from app.research.lab.backtest import BacktestEngine, DecisionBacktestEngine
        from app.research.lab.config import DecisionRunConfig
        from tests.research_data import M15, Lab
        lab = Lab(path=path)
        try:
            rp = lab.replay()
            cs = {"ETHUSDT": lab.cs}
            v1 = BacktestEngine(bt).run(rp, cs, M15, {})
            run = DecisionRunConfig.from_mapping({"schema": 1, "initial_equity_quote": bt.initial_equity_quote,
                                                  "costs": bt.costs.canonical()})
            v9 = DecisionBacktestEngine(run).run_gate(rp, cs, M15, {}, CompatGate(bt), {}, run.config_hash)
        finally:
            lab.close()
        strip = lambda ts: [{k: v for k, v in t.to_dict().items() if k != "run_id"} for t in ts]  # noqa: E731
        self.assertEqual(strip(v9.trades), strip(v1.trades))
        self.assertEqual(v9.equity, v1.equity)
        return v1

    def test_equivalence_on_several_paths_and_costs(self):
        from app.research.lab.config import BacktestConfig
        from tests.research_data import BCFG, PATH_BT, bcfg
        from tests.setup_data import PATH_P, PATH_R, PATH_S1
        from tests.structure_data import mirror
        full = BacktestConfig.from_file(BCFG)                                 # slippage, fees, assumed funding
        cases = [("PATH_BT fixture", PATH_BT, bcfg()), ("PATH_BT default v1", PATH_BT, full),
                 ("mirror short", mirror(PATH_BT), full), ("all types", PATH_BT, bcfg(setup_types=[
                     "breakout", "pullback", "sweep_reversal", "range_rejection"])),
                 ("pullback path", PATH_P + [(112.5, 110, 112), (113, 110.5, 111)] * 3, full),
                 ("sweep path", PATH_S1 + [(99.6, 97.4, 98.5), (98.6, 97, 97.5)] * 3, full),
                 ("range path", PATH_R + [(106, 103, 104), (105, 102, 103)] * 3, full)]
        traded = 0
        for name, path, bt in cases:
            with self.subTest(case=name):
                traded += len(self.compare(path, bt).trades)
        self.assertGreaterEqual(traded, 5, "the equivalence must be exercised on real trades")


class Phase7CompatibilityTests(unittest.TestCase):
    """Criterion D: identities recorded on the accepted Phase 8 archive BEFORE any Phase 9 change
    (step 1). Phase 9 must reproduce them bit for bit."""
    REFERENCE = {
        "component_versions": {"code": "0.7.0", "feature_catalog": "25d5762ed822baa9",
                               "regime": "1:f9cd248bb194a1eb", "structure": "1:943549ca365f1e19",
                               "setup": "1:0cd3c37ea9977d57"},
        "dataset_id": "ds_82fd659871da1986c0f6", "replay_run_id": "rp_f962f4fc0e377a1b61c8",
        "replay_hash": "89389e750863467a93f943c6",
        "default_v1": ("bt_25f1b27ebe83b908fcbf", "33a3bb81b075efc210e641c668c103b1"),
        "fixture_v1": ("bt_358ec00f1cde3c34bce2", "f61dce680b104d35e3f425a9c6726197"),
    }

    def test_phase7_identities_unchanged(self):
        from app.research.lab.config import BacktestConfig
        from app.research.lab.versions import component_versions
        from tests.research_data import BCFG, Lab, bcfg
        ref = self.REFERENCE
        self.assertEqual(component_versions(), ref["component_versions"])
        lab = Lab()
        try:
            ds = lab.dataset()
            rp, _ = lab.pipe.replay(ds.dataset_id)
            self.assertEqual((ds.dataset_id, rp.run_id, rp.replay_hash),
                             (ref["dataset_id"], ref["replay_run_id"], ref["replay_hash"]))
            for key, cfg in (("default_v1", BacktestConfig.from_file(BCFG)), ("fixture_v1", bcfg())):
                res, _, created = lab.pipe.backtest(rp.run_id, cfg)
                self.assertEqual((res.run_id, lab.store.run(res.run_id)["result_hash"]), ref[key], key)
                self.assertFalse(lab.pipe.backtest(rp.run_id, cfg)[2])          # rerun: identical -> no-op
        finally:
            lab.close()


def make_gate(mode="equity_pct", quote=None, with_rules=True, **strategy_changes):
    from app.config.settings import RiskSettings
    from app.risk.config import RiskConfig
    from app.risk.engine import RiskEngineV1
    from app.strategy.pipeline import StrategyRiskGate
    ins = {"ETHUSDT": rules()} if with_rules else {}
    return StrategyRiskGate(strat(**strategy_changes), RiskEngineV1(RiskConfig.from_settings(RiskSettings(), mode,
                                                                                            quote)), ins)


class PipelineRunTests(unittest.TestCase):
    """Step 7. PATH_BT breakout: stop 107.106708901324538, decision price 109 -> 1.893291098675462 per unit;
    budget 0.5 % of 10000 = 50 -> 26.408... -> 26.40 (step 0.01)."""

    @classmethod
    def setUpClass(cls):
        from app.research.lab.config import DecisionRunConfig
        from tests.research_data import Lab
        from tests.structure_data import ROOT
        cls.lab = Lab()
        cls.rp = cls.lab.replay()
        cls.run_cfg = DecisionRunConfig.from_file(ROOT / "config" / "decision_run_v1.json")

    @classmethod
    def tearDownClass(cls):
        cls.lab.close()

    def run_gate(self, gate):
        return self.lab.pipe.decide(self.rp.run_id, gate, self.run_cfg)

    def test_equity_pct_run_by_hand_and_idempotent(self):
        res, rep, _ = self.run_gate(make_gate(setup_types=["breakout"]))   # may already exist (test order)
        (t,) = res.trades
        self.assertEqual((t.setup_type, t.qty), ("breakout", 26.40))
        self.assertAlmostEqual(t.stop, 107.10670890132454)
        again = self.run_gate(make_gate(setup_types=["breakout"]))
        self.assertEqual((again[0].run_id, again[2]), (res.run_id, False))          # identical rerun -> no-op
        run = self.lab.store.run(res.run_id)
        self.assertEqual((run["kind"], run["config"]["pipeline"]), ("backtest", "strategy_risk_v1"))
        g = run["config"]["gate"]
        self.assertEqual((g["strategy"]["version"], g["risk"]["model_version"], g["risk"]["config"]["mode"]),
                         ("1", "1", "equity_pct"))
        self.assertEqual(g["instrument_rules"]["ETHUSDT"]["qty_step"], "0.01")
        self.assertIn("decisions", run["reports"])
        outcomes = [d["outcome"] for d in run["reports"]["decisions"]["decisions"]]
        self.assertEqual(outcomes.count("entered"), 1)
        # target = 109.0218 + 2 * (109.0218 - 107.1067...) = 112.852 > bar-17 high 112: the breakout is still open
        # when the pullback is decided (open of bar 18) -> skipped before the strategy, as in Phase 7
        self.assertEqual(rep["summary"]["skipped_signals"], {"not_decidable": 1, "position_already_open": 1})

    def test_missing_instrument_rules_rejects_in_equity_pct(self):
        res, rep, _ = self.run_gate(make_gate(with_rules=False, setup_types=["breakout"]))
        self.assertEqual(res.trades, [])
        self.assertEqual(rep["summary"]["skipped_signals"]["risk:instrument_rules_unavailable"], 1)
        self.assertTrue(any(w.startswith("INSTRUMENT_RULES_MISSING") for w in rep["summary"]["warnings"]))

    def test_config_changes_give_new_runs(self):
        a = self.run_gate(make_gate(setup_types=["breakout"]))[0].run_id
        b = self.run_gate(make_gate("fixed_quote", 100, setup_types=["breakout"]))[0].run_id
        c = self.run_gate(make_gate(setup_types=["breakout"], take_profit_r=3))[0].run_id
        self.assertEqual(len({a, b, c}), 3)

    def test_no_new_tables(self):
        tables = {r[0] for r in self.lab.db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(len(tables), 39)
        self.assertEqual(self.lab.db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0],
                         "0008_research")


class AnalyticsFunnelTests(unittest.TestCase):
    """Step 8. Hand example: 10 observed; not_decidable 2; no_next_bar 1, position_already_open 1 -> 6 evaluated;
    strategy rejections 2 -> 4 accepted; risk rejections 1 -> 3 approved; opened_beyond_stop 1 -> 2 entered;
    1 profitable."""

    def test_decision_funnel_by_hand(self):
        from app.analytics import engine
        from tests.analytics_data import trade
        summ = {"pipeline": "strategy_risk_v1", "skipped_signals": {
            "not_decidable": 2, "no_next_bar": 1, "position_already_open": 1, "strategy:unsupported_setup": 1,
            "strategy:conflicting_setup": 1, "risk:open_risk_limit": 1, "opened_beyond_stop": 1}}
        f = engine.research_funnel([{}] * 10, summ, [trade(1, 5.0), trade(2, -1.0)])
        self.assertEqual([(s["step"], s["count"]["value"]) for s in f["steps"]],
                         [("observed_setups", 10), ("decidable", 8), ("evaluated", 6), ("strategy_accepted", 4),
                          ("risk_approved", 3), ("entered", 2), ("profitable", 1)])
        self.assertEqual(f["warnings"], [])
        self.assertEqual(f["risk_rejections"], {"risk:open_risk_limit": 1})
        self.assertNotIn("risk_approved", f["not_recorded_stages"])          # now recorded for research runs

    def test_real_decision_run_is_consistent_and_phase7_unchanged(self):
        from app.analytics.engine import AnalyticsEngine
        from app.research.lab.config import DecisionRunConfig
        from app.research.lab.versions import component_versions
        from app.storage.analytics_read import SQLiteAnalyticsReader
        from tests.research_data import Lab, bcfg
        from tests.structure_data import ROOT
        lab = Lab()
        try:
            rp = lab.replay()
            res, _, _ = lab.pipe.decide(rp.run_id, make_gate(), DecisionRunConfig.from_file(
                ROOT / "config" / "decision_run_v1.json"))
            v1, _, _ = lab.pipe.backtest(rp.run_id, bcfg())
            r = SQLiteAnalyticsReader(lab.db.path)
            eng = AnalyticsEngine(r, component_versions())
            f9 = eng.research("funnel", res.run_id)
            f7 = eng.research("funnel", v1.run_id)
            r.close()
        finally:
            lab.close()
        self.assertEqual(f9["warnings"], [])
        self.assertEqual(f9["data"]["steps"][4]["step"], "risk_approved")
        self.assertEqual([s["step"] for s in f7["data"]["steps"]],                    # Phase 7 runs: as before
                         ["observed_setups", "passed_rule_filters", "entered", "profitable"])


class Phase9CliTests(unittest.TestCase):
    """Step 9: research decide / decisions on a fixture DB (instrument rules stored in `instruments`)."""

    @classmethod
    def setUpClass(cls):
        from app.domain.market import InstrumentInfo, Symbol
        from tests.research_data import Lab
        cls.lab = Lab()
        cls.rp = cls.lab.replay()
        cls.lab.db.store.upsert_instruments([InstrumentInfo(Symbol("ETHUSDT"), D("0.01"), D("0.01"), D("0.01"),
                                                            D("5"), max_leverage=D("100"))], "fixture")
        cls.env = {"DB_PATH": str(cls.lab.db.path)}

    @classmethod
    def tearDownClass(cls):
        cls.lab.close()

    def cli(self, *argv):
        import contextlib
        import io
        import os
        from unittest import mock
        from app.cli.main import main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.dict(os.environ, self.env):
            code = main(["research", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_decide_and_decisions(self):
        code, out, err = self.cli("decide", "--replay", self.rp.run_id)
        self.assertEqual(code, 0, err)
        self.assertIn("mode=equity_pct", out)
        self.assertIn("RESEARCH / SIMULATION", out)
        run_id = out.split("strategy+risk ")[1].split()[0]
        code2, out2, _ = self.cli("decide", "--replay", self.rp.run_id)
        self.assertIn("identical rerun", out2)
        code, out, _ = self.cli("decisions", "--id", run_id)
        self.assertEqual(code, 0)
        self.assertIn("entered", out)
        self.assertIn("strategy=strategy:accepted", out)
        run = self.lab.store.run(run_id)
        self.assertEqual(run["config"]["gate"]["instrument_rules"]["ETHUSDT"]["min_notional"], "5")

    def test_fixed_quote_via_cli(self):
        code, out, err = self.cli("decide", "--replay", self.rp.run_id, "--risk-mode", "fixed_quote",
                                  "--fixed-risk-quote", "100")
        self.assertEqual(code, 0, err)
        self.assertIn("mode=fixed_quote", out)

    def test_errors(self):
        self.assertEqual(self.cli("decide", "--replay", self.rp.run_id, "--risk-mode", "fixed_quote")[0], 2)
        self.assertEqual(self.cli("decide", "--replay", self.rp.run_id, "--fixed-risk-quote", "5")[0], 2)
        self.assertEqual(self.cli("decide", "--replay", "rp_missing")[0], 2)
        self.assertEqual(self.cli("decisions", "--id", "bt_missing")[0], 1)


class LegacyRetirementTests(unittest.TestCase):
    """Step 10 (D9-1): old Phase 0 types stay physically, are marked deprecated, and no port uses them."""

    def test_ports_use_only_phase9_contracts(self):
        import ast
        from tests.structure_data import ROOT
        for f in ("app/strategy/ports.py", "app/risk/ports.py", "app/research/lab/ports.py", "app/strategy/engine.py",
                  "app/risk/engine.py", "app/strategy/pipeline.py"):
            names = {a.name for n in ast.walk(ast.parse((ROOT / f).read_text())) if isinstance(n, ast.ImportFrom)
                     for a in n.names}
            self.assertFalse({"Signal", "ResearchSnapshot", "EntryPlan"} & names, f)
            self.assertNotIn("Setup", names, f)                              # only SetupView / setup.Setup

    def test_deprecated_but_present(self):
        from app.domain import decision, research
        for cls in (decision.Signal, decision.Setup, research.ResearchSnapshot):
            self.assertIn("DEPRECATED", cls.__doc__, cls.__name__)
        import app.domain as dom
        self.assertIn("SetupView", dom.__doc__)
