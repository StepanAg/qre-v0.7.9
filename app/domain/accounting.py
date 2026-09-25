"""Accounting. A Trade is the economic lifecycle of one position episode,
rebuilt from immutable legs (fills) and funding events. PnL is always DERIVED,
never stored as an overwritten number."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from app.domain.common import dec, req, utc
from app.domain.decision import RiskPlan
from app.domain.enums import ExitReason, FillRole, PositionSide, TradeStatus
from app.domain.errors import DomainError, InvariantViolation
from app.domain.execution import Fill
from app.domain.market import Symbol

ZERO = Decimal("0")


@dataclass(frozen=True)
class TradeLeg:
    exec_id: str
    role: FillRole
    qty: Decimal
    price: Decimal
    fee: Decimal
    fee_asset: str
    exec_time: datetime
    is_maker: bool
    expected_price: Decimal | None = None


@dataclass(frozen=True)
class FundingEvent:
    funding_id: str
    symbol: Symbol
    amount: Decimal          # signed: + received, - paid
    asset: str
    occurred_at: datetime
    exchange_ref: str        # dedup key from exchange transaction log
    rate: Decimal | None = None
    trade_id: str | None = None

    def __post_init__(self) -> None:
        req("exchange_ref", self.exchange_ref)
        utc("occurred_at", self.occurred_at)
        object.__setattr__(self, "amount", dec("amount", self.amount))


@dataclass(frozen=True)
class PnLBreakdown:
    """theoretical_gross - slippage_cost = gross (actual fills);
    gross - fees + funding = net. Slippage is already inside `gross`, so it is
    shown for attribution and never subtracted twice."""
    theoretical_gross: Decimal
    slippage_cost: Decimal
    gross: Decimal
    fees: Decimal
    funding: Decimal
    net: Decimal


@dataclass(frozen=True)
class Trade:
    trade_id: str
    symbol: Symbol
    side: PositionSide
    risk_plan: RiskPlan
    legs: tuple[TradeLeg, ...] = ()
    funding: tuple[FundingEvent, ...] = ()
    exit_reason: ExitReason | None = None
    signal_id: str | None = None

    def __post_init__(self) -> None:
        req("trade_id", self.trade_id)
        if self.risk_plan.side is not self.side:
            raise DomainError("risk_plan side mismatch")
        self._replay()  # validates invariants of the whole leg sequence

    # ------------------------------------------------------------ replay
    def _ordered_legs(self) -> list[TradeLeg]:
        return sorted(self.legs, key=lambda l: (l.exec_time, l.role is FillRole.EXIT, l.exec_id))

    def _replay(self) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
        """Returns (open_qty, avg_entry, gross, slippage_cost, max_open_qty)."""
        open_qty = avg = gross = slip = max_open = ZERO
        closed = False
        sign = self.side.sign
        for leg in self._ordered_legs():
            if closed:
                raise InvariantViolation(f"{self.trade_id}: fill {leg.exec_id} after trade closed")
            if leg.expected_price is not None:
                # positive = worse than expected for us
                direction = sign if leg.role is FillRole.ENTRY else -sign
                slip += (leg.price - leg.expected_price) * leg.qty * direction
            if leg.role is FillRole.ENTRY:
                avg = (avg * open_qty + leg.price * leg.qty) / (open_qty + leg.qty)
                open_qty += leg.qty
                max_open = max(max_open, open_qty)
            else:
                if leg.qty > open_qty:
                    raise InvariantViolation(
                        f"{self.trade_id}: exit {leg.qty} > open {open_qty} (duplicate/phantom close?)"
                    )
                gross += (leg.price - avg) * leg.qty * sign
                open_qty -= leg.qty
                if open_qty == 0:
                    closed = True
        return open_qty, avg, gross, slip, max_open

    # ------------------------------------------------------------ commands
    def apply_fill(self, fill: Fill, role: FillRole) -> "Trade":
        """Idempotent: re-applying the same exec_id returns the trade unchanged."""
        if any(l.exec_id == fill.exec_id for l in self.legs):
            return self
        if fill.symbol != self.symbol:
            raise InvariantViolation("fill symbol does not match trade")
        expected_side = self.side.entry_side if role is FillRole.ENTRY else self.side.entry_side.opposite
        if fill.side is not expected_side:
            raise InvariantViolation(f"{role.value} fill must be {expected_side.value}")
        leg = TradeLeg(fill.exec_id, role, fill.qty, fill.price, fill.fee, fill.fee_asset,
                       fill.exec_time, fill.is_maker, fill.expected_price)
        return replace(self, legs=self.legs + (leg,))

    def apply_funding(self, ev: FundingEvent) -> "Trade":
        if any(f.exchange_ref == ev.exchange_ref for f in self.funding):
            return self
        if ev.symbol != self.symbol:
            raise InvariantViolation("funding symbol does not match trade")
        return replace(self, funding=self.funding + (ev,))

    def close_with_reason(self, reason: ExitReason) -> "Trade":
        if self.status is not TradeStatus.CLOSED:
            raise InvariantViolation("exit reason can only be set on a closed trade")
        if self.exit_reason is not None and self.exit_reason is not reason:
            raise InvariantViolation(f"exit reason already {self.exit_reason.value}")
        return replace(self, exit_reason=reason)

    # ------------------------------------------------------------ queries
    @property
    def status(self) -> TradeStatus:
        open_qty, *_ = self._replay()
        if not self.legs:
            return TradeStatus.PLANNED
        has_exit = any(l.role is FillRole.EXIT for l in self.legs)
        return TradeStatus.CLOSED if open_qty == 0 and has_exit else TradeStatus.OPEN

    @property
    def open_qty(self) -> Decimal:
        return self._replay()[0]

    @property
    def avg_entry(self) -> Decimal:
        return self._replay()[1]

    def pnl(self) -> PnLBreakdown:
        _, _, gross, slip, _ = self._replay()
        fees = sum((l.fee for l in self.legs), ZERO)
        funding = sum((f.amount for f in self.funding), ZERO)
        return PnLBreakdown(
            theoretical_gross=gross + slip, slippage_cost=slip, gross=gross,
            fees=fees, funding=funding, net=gross - fees + funding,
        )

    @property
    def initial_risk_amount(self) -> Decimal:
        """Actual initial risk: every ENTRY leg measured against the FROZEN
        initial stop. Moving the stop later cannot change this number."""
        stop = self.risk_plan.initial_stop
        return sum((abs(l.price - stop) * l.qty for l in self.legs if l.role is FillRole.ENTRY), ZERO)

    def realized_r(self) -> Decimal | None:
        """Net realized PnL / initial risk. None until closed or if risk is zero."""
        if self.status is not TradeStatus.CLOSED:
            return None
        risk = self.initial_risk_amount
        if risk == 0:
            return None
        return self.pnl().net / risk
