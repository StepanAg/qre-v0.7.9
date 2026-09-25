"""Execution entities. An Order is an instruction; a Fill is a fact reported by
the exchange (or simulator). Neither is a Trade, and neither carries PnL."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Iterable

from app.domain.common import dec, req, utc
from app.domain.enums import (
    Category, EventSource, OrderStatus, OrderType, PositionEventType, PositionSide,
    ReconciliationStatus, Side, TimeInForce,
)
from app.domain.errors import DomainError, InvalidTransition
from app.domain.market import Symbol

S = OrderStatus
_ALLOWED: dict[OrderStatus, set[OrderStatus]] = {
    S.CREATED: {S.SUBMITTING, S.REJECTED, S.CANCELLED},
    S.SUBMITTING: {S.ACCEPTED, S.PARTIALLY_FILLED, S.FILLED, S.REJECTED, S.UNKNOWN, S.CANCELLED},
    S.ACCEPTED: {S.PARTIALLY_FILLED, S.FILLED, S.CANCEL_REQUESTED, S.CANCELLED, S.EXPIRED, S.UNKNOWN},
    S.PARTIALLY_FILLED: {S.PARTIALLY_FILLED, S.FILLED, S.CANCEL_REQUESTED, S.CANCELLED, S.EXPIRED, S.UNKNOWN},
    S.CANCEL_REQUESTED: {S.CANCELLED, S.FILLED, S.PARTIALLY_FILLED, S.UNKNOWN},
    S.UNKNOWN: {S.ACCEPTED, S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.REJECTED, S.EXPIRED},
    S.FILLED: set(), S.CANCELLED: set(), S.REJECTED: set(), S.EXPIRED: set(),
}


@dataclass(frozen=True)
class Order:
    local_order_id: str
    client_order_id: str            # sent to exchange (orderLinkId); persisted before submit
    symbol: Symbol
    side: Side
    order_type: OrderType
    qty: Decimal
    created_at: datetime
    updated_at: datetime
    price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.GTC
    reduce_only: bool = False
    trade_id: str | None = None
    signal_id: str | None = None
    exchange_order_id: str | None = None
    status: OrderStatus = OrderStatus.CREATED
    source: EventSource = EventSource.LOCAL
    reconciliation_status: ReconciliationStatus = ReconciliationStatus.PENDING

    def __post_init__(self) -> None:
        req("local_order_id", self.local_order_id)
        req("client_order_id", self.client_order_id)
        if len(self.client_order_id) > 36:
            raise DomainError("client_order_id exceeds 36 chars (Bybit orderLinkId limit)")
        utc("created_at", self.created_at)
        utc("updated_at", self.updated_at)
        object.__setattr__(self, "qty", dec("qty", self.qty, positive=True))
        if self.order_type is OrderType.LIMIT:
            if self.price is None:
                raise DomainError("LIMIT order requires price")
            object.__setattr__(self, "price", dec("price", self.price, positive=True))

    def transition(self, new: OrderStatus, at: datetime, **changes) -> "Order":
        if new not in _ALLOWED[self.status]:
            raise InvalidTransition(f"{self.local_order_id}: {self.status.value} -> {new.value}")
        return replace(self, status=new, updated_at=utc("at", at), **changes)


@dataclass(frozen=True)
class OrderEvent:
    """Append-only history of an order. idempotency_key dedups WS/REST repeats."""
    event_id: str
    local_order_id: str
    status: OrderStatus
    occurred_at: datetime
    source: EventSource
    idempotency_key: str
    detail: str = ""


@dataclass(frozen=True)
class Fill:
    """One execution. exec_id is the exchange execution id (globally unique) and is
    the dedup key: the same fill arriving via WS and REST is stored once."""
    exec_id: str
    symbol: Symbol
    side: Side
    qty: Decimal
    price: Decimal
    fee: Decimal                   # positive = cost, negative = rebate
    fee_asset: str
    is_maker: bool
    exec_time: datetime
    source: EventSource
    local_order_id: str | None = None      # None => fill of an order we did not create
    exchange_order_id: str | None = None
    expected_price: Decimal | None = None  # for slippage attribution

    def __post_init__(self) -> None:
        req("exec_id", self.exec_id)
        req("fee_asset", self.fee_asset)
        utc("exec_time", self.exec_time)
        object.__setattr__(self, "qty", dec("qty", self.qty, positive=True))
        object.__setattr__(self, "price", dec("price", self.price, positive=True))
        object.__setattr__(self, "fee", dec("fee", self.fee))
        if self.expected_price is not None:
            object.__setattr__(self, "expected_price", dec("expected_price", self.expected_price, positive=True))

    @property
    def notional(self) -> Decimal:
        return self.qty * self.price


def filled_qty(order: Order, fills: Iterable[Fill]) -> Decimal:
    """Filled quantity is derived from fills, never stored as a counter."""
    seen: set[str] = set()
    total = Decimal("0")
    for f in fills:
        if f.local_order_id != order.local_order_id or f.exec_id in seen:
            continue
        seen.add(f.exec_id)
        total += f.qty
    if total > order.qty:
        raise DomainError(f"{order.local_order_id}: fills {total} exceed order qty {order.qty}")
    return total


@dataclass(frozen=True)
class Position:
    """A snapshot of exposure. Local and exchange positions are separate objects
    tagged by source; they are compared by reconciliation, never merged blindly."""
    symbol: Symbol
    side: PositionSide
    qty: Decimal
    avg_entry: Decimal
    as_of: datetime
    source: EventSource
    stop_loss: Decimal | None = None

    def __post_init__(self) -> None:
        utc("as_of", self.as_of)
        object.__setattr__(self, "qty", dec("qty", self.qty, non_negative=True))


@dataclass(frozen=True)
class PositionEvent:
    event_id: str
    symbol: Symbol
    side: PositionSide
    event_type: PositionEventType
    qty_delta: Decimal
    price: Decimal
    occurred_at: datetime
    source: EventSource
    exec_id: str | None = None  # the fill that caused it (None for SL moves etc.)
    trade_id: str | None = None
