"""Risk Engine v1: StrategyDecision + decision price + portfolio + instrument rules ->
RiskDecision. Pure and deterministic; never executes, never closes positions.

Fixed check order (part of the Phase 9 contract):
  equity_pct:
    1 global stops    hard DD -> HALTED risk:hard_drawdown_halt; kill switch -> REJECTED
                      risk:kill_switch_active; daily loss -> risk:daily_loss_limit;
                      open positions -> risk:max_open_positions
    2 stop side       LONG stop < price, SHORT stop > price, else risk:stop_on_wrong_side
    3 instrument      rules required, else risk:instrument_rules_unavailable
    4 size            qty = equity * risk% / |price - stop|, rounded DOWN to qty_step
    5 leverage        notional > cap -> qty REDUCED to the cap (risk:reduced_leverage_cap)
    6 minimums        AFTER every reduction: qty < min_qty or notional < min_notional ->
                      risk:below_min_size (risk is never increased to pass a minimum)
    7 open risk       open risk + new risk > limit -> risk:open_risk_limit (no reduction)
  fixed_quote (Phase 7 compatibility only):
    2 stop side, then qty = fixed quote / |price - stop|; no rounding, no other check;
    instrument rules, if given, are recorded as diagnostics and never applied.
"""
from __future__ import annotations

import hashlib
from decimal import ROUND_FLOOR, Decimal

from app.domain.decision import (InstrumentRules, PortfolioState, RiskCheck, RiskDecision, RiskPlan,
                                 StrategyDecision)
from app.domain.enums import PositionSide, RiskStatus
from app.risk.config import RiskConfig

RISK_MODEL_VERSION = "1"
_ZERO = Decimal(0)


def floor_to_step(qty: Decimal, step: Decimal) -> Decimal:
    return (qty / step).to_integral_value(rounding=ROUND_FLOOR) * step


class RiskEngineV1:
    def __init__(self, config: RiskConfig) -> None:
        self.cfg = config
        self.model_version = RISK_MODEL_VERSION
        self.config_hash = config.config_hash

    def _id(self, d: StrategyDecision, price: Decimal, p: PortfolioState) -> str:
        raw = "|".join((d.decision_id, str(price), str(p.equity), str(p.peak_equity), str(p.open_risk),
                        str(len(p.open_positions)), str(p.realized_today), str(p.halted), str(p.kill_switch),
                        self.model_version, self.config_hash))
        return "rd_" + hashlib.sha256(raw.encode()).hexdigest()[:24]

    def assess(self, d: StrategyDecision, reference_price: Decimal, portfolio: PortfolioState,
               instrument: InstrumentRules | None) -> RiskDecision:
        if not d.accepted:
            raise ValueError("the Risk Engine assesses accepted strategy decisions only")
        price = Decimal(str(reference_price)) if not isinstance(reference_price, Decimal) else reference_price
        checks: list[RiskCheck] = []
        reasons: list[str] = []
        diag = tuple(sorted(instrument.to_dict().items())) if (instrument and self.cfg.mode == "fixed_quote") else ()

        def out(status, code, qty=_ZERO, plan=None):
            if code not in reasons:
                reasons.append(code)
            per = abs(price - d.stop_reference)
            return RiskDecision(self._id(d, price, portfolio), d.setup_id, status, code, tuple(reasons), plan, qty,
                                qty * per, qty * price, tuple(checks), self.cfg.mode, self.model_version,
                                self.config_hash, diag)

        cfg, p = self.cfg, portfolio
        long = d.direction is PositionSide.LONG
        if cfg.mode == "equity_pct":
            dd = (p.peak_equity - p.equity) / p.peak_equity if p.peak_equity > 0 else _ZERO
            hard, kill = cfg.frac(cfg.hard_drawdown_limit_pct), cfg.frac(cfg.kill_switch_drawdown_pct)
            ok = not p.halted and dd < hard
            checks.append(RiskCheck("hard_drawdown", ok, str(dd), f"<{hard}"))
            if not ok:
                return out(RiskStatus.HALTED, "risk:hard_drawdown_halt")
            ok = not p.kill_switch and dd < kill
            checks.append(RiskCheck("kill_switch", ok, str(dd), f"<{kill}"))
            if not ok:
                return out(RiskStatus.REJECTED, "risk:kill_switch_active")
            limit = cfg.frac(cfg.daily_loss_limit_pct) * p.day_start_equity
            ok = -p.realized_today < limit
            checks.append(RiskCheck("daily_loss", ok, str(-p.realized_today), f"<{limit}"))
            if not ok:
                return out(RiskStatus.REJECTED, "risk:daily_loss_limit")
            ok = len(p.open_positions) < cfg.max_open_positions
            checks.append(RiskCheck("open_positions", ok, str(len(p.open_positions)), f"<{cfg.max_open_positions}"))
            if not ok:
                return out(RiskStatus.REJECTED, "risk:max_open_positions")
        stop = d.stop_reference
        ok = stop < price if long else stop > price
        checks.append(RiskCheck("stop_side", ok, str(stop), f"{'<' if long else '>'}{price}"))
        if not ok:
            return out(RiskStatus.REJECTED, "risk:stop_on_wrong_side")
        per_unit = abs(price - stop)
        if cfg.mode == "fixed_quote":
            qty = cfg.fixed_risk_quote / per_unit                     # Phase 7 semantics, no rounding
            checks.append(RiskCheck("size_fixed_quote", True, str(qty), str(cfg.fixed_risk_quote)))
            return out(RiskStatus.APPROVED, "risk:approved", qty, self._plan(d, price, qty))
        ok = instrument is not None
        checks.append(RiskCheck("instrument_rules", ok, "present" if ok else "missing", "required"))
        if not ok:
            return out(RiskStatus.REJECTED, "risk:instrument_rules_unavailable")
        budget = p.equity * cfg.frac(cfg.risk_per_trade_pct)
        qty = floor_to_step(budget / per_unit, instrument.qty_step)
        checks.append(RiskCheck("size", True, str(qty), f"budget={budget}"))
        lev = cfg.max_leverage if instrument.max_leverage is None else min(cfg.max_leverage, instrument.max_leverage)
        cap = lev * p.equity
        ok = qty * price <= cap
        checks.append(RiskCheck("leverage", ok, str(qty * price), f"<={cap}"))
        if not ok:
            qty = floor_to_step(cap / price, instrument.qty_step)
            reasons.append("risk:reduced_leverage_cap")
        notional = qty * price
        ok = qty > 0 and qty >= instrument.min_qty and (instrument.min_notional is None
                                                        or notional >= instrument.min_notional)
        checks.append(RiskCheck("minimums", ok, f"qty={qty} notional={notional}",
                                f"min_qty={instrument.min_qty} min_notional={instrument.min_notional}"))
        if not ok:
            return out(RiskStatus.REJECTED, "risk:below_min_size")
        new_risk, limit = qty * per_unit, cfg.frac(cfg.max_open_risk_pct) * p.equity
        ok = p.open_risk + new_risk <= limit
        checks.append(RiskCheck("open_risk", ok, str(p.open_risk + new_risk), f"<={limit}"))
        if not ok:
            return out(RiskStatus.REJECTED, "risk:open_risk_limit")
        return out(RiskStatus.APPROVED, "risk:approved", qty, self._plan(d, price, qty))

    def _plan(self, d: StrategyDecision, price: Decimal, qty: Decimal) -> RiskPlan:
        # RiskPlan (Phase 0) unchanged: planned_entry = decision price, initial_stop frozen (R anchor)
        return RiskPlan("rp_" + d.decision_id[3:], d.setup_id, d.direction, price, d.stop_reference, qty)
