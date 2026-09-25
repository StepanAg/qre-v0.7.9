"""Trading decision entities. None of these is an order or a trade."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.domain.common import dec, req, utc
from app.domain.enums import Direction, OrderType, PositionSide, RiskStatus, SetupStatus, TimeInForce
from app.domain.errors import DomainError
from app.domain.market import InstrumentInfo, Symbol


@dataclass(frozen=True)
class Signal:
    """DEPRECATED (Phase 9, decision D9-1): not used by the Strategy/Risk contracts;
    kept physically for compatibility. Its `strength` score has no defined meaning."""
    """An opinion about the market. Cheap, frequent, may be discarded."""
    signal_id: str
    symbol: Symbol
    direction: Direction
    created_at: datetime
    strategy_id: str
    strategy_version: str
    strength: Decimal
    research_snapshot_id: str | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        req("signal_id", self.signal_id)
        req("strategy_id", self.strategy_id)
        utc("created_at", self.created_at)
        object.__setattr__(self, "strength", dec("strength", self.strength))


@dataclass(frozen=True)
class Setup:
    """DEPRECATED (Phase 9, decisions F1/D9-1): the canonical setup is
    app.domain.setup.Setup (Phase 6). Kept physically; no port uses it."""
    """A signal that passed setup filters and is worth planning."""
    setup_id: str
    signal_id: str
    side: PositionSide
    setup_type: str
    valid_until: datetime

    def __post_init__(self) -> None:
        req("setup_id", self.setup_id)
        req("signal_id", self.signal_id)
        utc("valid_until", self.valid_until)


@dataclass(frozen=True)
class EntryPlan:
    """Where and how to enter. Maker timeout is a deadline, not a blocking wait."""
    entry_plan_id: str
    setup_id: str
    zone_low: Decimal
    zone_high: Decimal
    order_type: OrderType
    time_in_force: TimeInForce
    expires_at: datetime                 # maker timeout deadline
    fallback_to_market: bool = False
    max_slippage_bps: Decimal = Decimal("10")

    def __post_init__(self) -> None:
        req("entry_plan_id", self.entry_plan_id)
        utc("expires_at", self.expires_at)
        lo = dec("zone_low", self.zone_low, positive=True)
        hi = dec("zone_high", self.zone_high, positive=True)
        if lo > hi:
            raise DomainError("zone_low > zone_high")
        object.__setattr__(self, "zone_low", lo)
        object.__setattr__(self, "zone_high", hi)


@dataclass(frozen=True)
class RiskPlan:
    """Frozen at planning time. initial_stop is the R denominator anchor and is
    NEVER modified; stop moves are separate events on the position."""
    risk_plan_id: str
    setup_id: str
    side: PositionSide
    planned_entry: Decimal
    initial_stop: Decimal
    planned_qty: Decimal
    take_profits: tuple[Decimal, ...] = ()

    def __post_init__(self) -> None:
        req("risk_plan_id", self.risk_plan_id)
        e = dec("planned_entry", self.planned_entry, positive=True)
        s = dec("initial_stop", self.initial_stop, positive=True)
        q = dec("planned_qty", self.planned_qty, positive=True)
        if self.side is PositionSide.LONG and s >= e:
            raise DomainError("LONG requires initial_stop < planned_entry")
        if self.side is PositionSide.SHORT and s <= e:
            raise DomainError("SHORT requires initial_stop > planned_entry")
        object.__setattr__(self, "planned_entry", e)
        object.__setattr__(self, "initial_stop", s)
        object.__setattr__(self, "planned_qty", q)

    @property
    def planned_risk_per_unit(self) -> Decimal:
        return abs(self.planned_entry - self.initial_stop)

    @property
    def planned_risk_amount(self) -> Decimal:
        return self.planned_risk_per_unit * self.planned_qty



# =============================================================== Phase 9 contracts
# Canonical input is the Phase 6 app.domain.setup.Setup, seen point-in-time.
# Strategy decides WHETHER and in WHICH direction; Risk decides IF and HOW MUCH.
# Neither executes anything. RiskPlan above is unchanged (AD7).

LONG, SHORT = PositionSide.LONG, PositionSide.SHORT


def side_of(direction: str) -> PositionSide:
    return {"bullish": LONG, "bearish": SHORT}[direction]


@dataclass(frozen=True)
class ConflictRef:
    setup_id: str
    setup_type: str
    direction: str


@dataclass(frozen=True)
class SetupView:
    """Point-in-time view of a Phase 6 setup at `decision_time`: only transitions with
    at <= decision_time; no contradictions / regime_fit computed later (look-ahead)."""
    setup_id: str
    symbol: str
    timeframe: str
    setup_type: str
    direction: str                      # bullish | bearish
    decision_time: datetime
    status_at_decision: SetupStatus
    setup_time: datetime
    confirmed_at: datetime | None
    key_level: float
    invalidation_condition: str
    evidence: tuple[tuple[str, object], ...]
    first_regime_fit: str               # allowed | not_allowed | unknown
    conflicting_active: tuple[ConflictRef, ...] = ()

    def __post_init__(self) -> None:
        req("setup_id", self.setup_id)
        utc("decision_time", self.decision_time)
        if self.direction not in ("bullish", "bearish"):
            raise DomainError("a setup view needs a known direction")
        if self.setup_time > self.decision_time:
            raise DomainError("setup not known at decision_time (look-ahead)")
        if self.confirmed_at is not None and self.confirmed_at > self.decision_time:
            raise DomainError("confirmation after decision_time (look-ahead)")
        if self.first_regime_fit not in ("allowed", "not_allowed", "unknown"):
            raise DomainError("invalid first_regime_fit")

    @property
    def evidence_map(self) -> dict:
        return dict(self.evidence)


def setup_view(setup, decision_time: datetime, first_regime_fit: str, others=()) -> SetupView:
    """Build a SetupView from a Phase 6 Setup (as last seen) keeping only what was known
    at decision_time. `others`: other Phase 6 setups (as last seen) of the same run."""
    def status_at(s) -> SetupStatus | None:
        known = [t for t in s.transitions if t.at <= decision_time]
        return known[-1].status if known else None
    st = status_at(setup)
    if st is None:
        raise DomainError(f"setup {setup.setup_id} is not known at {decision_time.isoformat()}")
    confirmed = next((t.at for t in setup.transitions if t.status is SetupStatus.CONFIRMED and t.at <= decision_time),
                     None)
    active = (SetupStatus.CANDIDATE, SetupStatus.CONFIRMED)
    conflicts = tuple(sorted(
        (ConflictRef(o.setup_id, o.setup_type.value, o.direction.value) for o in others
         if o.setup_id != setup.setup_id and o.symbol == setup.symbol and o.timeframe is setup.timeframe
         and o.direction is not setup.direction and status_at(o) in active),
        key=lambda c: c.setup_id))
    return SetupView(setup.setup_id, setup.symbol, setup.timeframe.value, setup.setup_type.value,
                     setup.direction.value, decision_time, st, setup.transitions[0].at, confirmed, setup.key_level,
                     setup.invalidation_condition, tuple(setup.evidence), first_regime_fit, conflicts)


STRATEGY_REASONS = ("strategy:accepted", "strategy:invalid_setup", "strategy:no_direction",
                    "strategy:conflicting_setup", "strategy:unsupported_setup", "strategy:missing_required_context")


@dataclass(frozen=True)
class StrategyDecision:
    decision_id: str                    # deterministic
    setup_id: str
    symbol: str
    decision_time: datetime
    accepted: bool
    direction: PositionSide | None      # None when not accepted
    reason_code: str                    # one of STRATEGY_REASONS
    reason_detail: str
    strategy_id: str
    strategy_version: str
    config_hash: str
    stop_reference: Decimal | None = None      # invalidation reference -/+ buffer (accepted only)
    take_profit_r: Decimal | None = None
    max_hold_bars: int | None = None

    def __post_init__(self) -> None:
        req("decision_id", self.decision_id)
        if self.reason_code not in STRATEGY_REASONS:
            raise DomainError(f"unknown strategy reason {self.reason_code}")
        if self.accepted != (self.reason_code == "strategy:accepted"):
            raise DomainError("accepted <=> reason strategy:accepted")
        if self.accepted:
            if self.direction is None or self.stop_reference is None or self.take_profit_r is None \
                    or self.max_hold_bars is None:
                raise DomainError("an accepted decision needs direction, stop_reference, take_profit_r, max_hold_bars")
            object.__setattr__(self, "stop_reference", dec("stop_reference", self.stop_reference, positive=True))
            object.__setattr__(self, "take_profit_r", dec("take_profit_r", self.take_profit_r, positive=True))
        elif self.direction is not None:
            raise DomainError("a rejected decision has no direction")


@dataclass(frozen=True)
class OpenExposure:
    symbol: str
    side: PositionSide
    initial_risk: Decimal
    notional: Decimal


@dataclass(frozen=True)
class PortfolioState:
    """Portfolio seen by the Risk Engine at the decision close (simulation state)."""
    as_of: datetime
    equity: Decimal
    peak_equity: Decimal
    day_start_equity: Decimal
    realized_today: Decimal
    open_positions: tuple[OpenExposure, ...] = ()
    halted: bool = False                # latched by a previous hard-drawdown decision
    kill_switch: bool = False           # latched by a previous kill-switch decision

    def __post_init__(self) -> None:
        utc("as_of", self.as_of)
        if self.peak_equity < self.equity:
            raise DomainError("peak_equity cannot be below equity")

    @property
    def open_risk(self) -> Decimal:
        return sum((p.initial_risk for p in self.open_positions), Decimal(0))


@dataclass(frozen=True)
class InstrumentRules:
    symbol: str
    qty_step: Decimal
    min_qty: Decimal
    min_notional: Decimal | None
    max_leverage: Decimal | None

    @classmethod
    def from_instrument(cls, i: InstrumentInfo) -> "InstrumentRules":
        return cls(i.symbol.name, i.qty_step, i.min_qty, i.min_notional, i.max_leverage)

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "qty_step": str(self.qty_step), "min_qty": str(self.min_qty),
                "min_notional": None if self.min_notional is None else str(self.min_notional),
                "max_leverage": None if self.max_leverage is None else str(self.max_leverage)}


@dataclass(frozen=True)
class RiskCheck:
    name: str
    passed: bool
    value: str
    limit: str


@dataclass(frozen=True)
class RiskDecision:
    decision_id: str
    setup_id: str
    status: RiskStatus
    reason_code: str                     # the deciding reason (risk:approved when approved)
    reason_codes: tuple[str, ...]        # every applied reason incl. reductions
    risk_plan: RiskPlan | None
    qty: Decimal
    risk_amount: Decimal
    notional: Decimal
    checks: tuple[RiskCheck, ...]
    mode: str                            # equity_pct | fixed_quote
    risk_model_version: str
    risk_config_hash: str
    instrument_diagnostic: tuple[tuple[str, object], ...] = ()   # fixed_quote: rules recorded, never applied

    def __post_init__(self) -> None:
        req("decision_id", self.decision_id)
        if not self.reason_code.startswith("risk:"):
            raise DomainError("risk reasons use the risk: namespace")
        if (self.status is RiskStatus.APPROVED) != (self.risk_plan is not None):
            raise DomainError("APPROVED <=> a RiskPlan is present")
        if self.mode not in ("equity_pct", "fixed_quote"):
            raise DomainError(f"unknown risk mode {self.mode}")


@dataclass(frozen=True)
class GateResult:
    setup_id: str
    strategy: StrategyDecision
    risk: RiskDecision | None            # None when the strategy did not accept
    enter: bool
    reason_code: str

    def __post_init__(self) -> None:
        if self.enter != (self.risk is not None and self.risk.status is RiskStatus.APPROVED):
            raise DomainError("enter <=> risk APPROVED")
        if self.risk is not None and not self.strategy.accepted:
            raise DomainError("risk is only assessed for accepted strategy decisions")
