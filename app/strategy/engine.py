"""Strategy v1: SetupView -> StrategyDecision. Pure and deterministic.

Decides WHETHER a setup is an admissible trade candidate and in WHICH direction. It
never sizes, never looks at price, balance, positions or instrument rules, and never
checks the stop side (the Risk Engine owns that: it has the decision price).
Rule order (first failing rule decides):
  1. setup type enabled                 else strategy:unsupported_setup
  2. lifecycle fact `entry_on` holds at decision_time and the setup is not closed
                                        else strategy:invalid_setup
  3. first_regime_fit allowed           else strategy:invalid_setup (regime_fit_not_allowed)
  4. ATR and the invalidation reference present in the evidence
                                        else strategy:missing_required_context
  5. no opposite-direction setup active at decision_time (if configured)
                                        else strategy:conflicting_setup
  -> strategy:accepted with direction, stop_reference, take_profit_r, max_hold_bars
The stop reference uses the same invalidation references and Decimal arithmetic as the
Phase 7 test rule (tests prove trade-for-trade equivalence).
"""
from __future__ import annotations

import hashlib
from decimal import Decimal

from app.domain.decision import SetupView, StrategyDecision, side_of
from app.domain.enums import PositionSide, SetupStatus
from app.strategy.config import StrategyConfig

STRATEGY_VERSION = "1"
_ACTIVE = (SetupStatus.CANDIDATE, SetupStatus.CONFIRMED)


def _d(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def invalidation_reference(v: SetupView) -> Decimal | None:
    """Price beyond which the setup is invalid (as in Phase 7): sweep -> sweep extreme;
    pullback -> level -/+ zone; breakout / range rejection -> key level."""
    ev = v.evidence_map
    bull = v.direction == "bullish"
    if v.setup_type == "sweep_reversal":
        return _d(ev["sweep_extreme"]) if "sweep_extreme" in ev else None
    if v.setup_type == "pullback":
        if "level" not in ev or "zone" not in ev:
            return None
        # float arithmetic, then Decimal(str(.)): exactly as the Phase 7 test rule (criterion E)
        return _d(float(ev["level"]) - float(ev["zone"]) if bull else float(ev["level"]) + float(ev["zone"]))
    return _d(float(v.key_level))


class StrategyV1:
    def __init__(self, config: StrategyConfig) -> None:
        self.cfg = config
        self.strategy_id = config.strategy_id
        self.version = STRATEGY_VERSION
        self.config_hash = config.config_hash

    def _id(self, v: SetupView) -> str:
        raw = "|".join((v.setup_id, v.decision_time.isoformat(), self.strategy_id, self.version, self.config_hash))
        return "sd_" + hashlib.sha256(raw.encode()).hexdigest()[:24]

    def _reject(self, v: SetupView, code: str, detail: str) -> StrategyDecision:
        return StrategyDecision(self._id(v), v.setup_id, v.symbol, v.decision_time, False, None, code, detail,
                                self.strategy_id, self.version, self.config_hash)

    def evaluate(self, v: SetupView) -> StrategyDecision:
        cfg = self.cfg
        if v.setup_type not in cfg.setup_types:
            return self._reject(v, "strategy:unsupported_setup", f"type_not_in_strategy:{v.setup_type}")
        if v.status_at_decision not in _ACTIVE:
            return self._reject(v, "strategy:invalid_setup", f"setup_closed:{v.status_at_decision.value}")
        fact_time = v.confirmed_at if cfg.entry_on == "confirmed" else v.setup_time
        if fact_time is None:
            return self._reject(v, "strategy:invalid_setup", "not_confirmed_at_decision")
        if v.first_regime_fit not in cfg.regime_fit_allowed:
            return self._reject(v, "strategy:invalid_setup", f"regime_fit_not_allowed:{v.first_regime_fit}")
        atr = v.evidence_map.get("atr")
        ref = invalidation_reference(v)
        if not atr or ref is None:
            return self._reject(v, "strategy:missing_required_context", "no_atr" if not atr else "no_invalidation_ref")
        if cfg.reject_conflicting_setups and v.conflicting_active:
            return self._reject(v, "strategy:conflicting_setup",
                                ",".join(f"{c.setup_type}:{c.direction}" for c in v.conflicting_active))
        side = side_of(v.direction)
        buf = _d(cfg.stop_buffer_atr) * _d(atr)
        stop = ref - buf if side is PositionSide.LONG else ref + buf
        if stop <= 0:
            return self._reject(v, "strategy:missing_required_context", "non_positive_stop_reference")
        return StrategyDecision(self._id(v), v.setup_id, v.symbol, v.decision_time, True, side, "strategy:accepted",
                                "", self.strategy_id, self.version, self.config_hash, stop, _d(cfg.take_profit_r),
                                cfg.max_hold_bars)
