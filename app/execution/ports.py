from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.domain.execution import Fill, Order, Position


class ExecutionGateway(Protocol):
    """Sends instructions. Returns nothing about PnL. Implementations MUST call
    OrderPermission.require() before any network request."""

    def submit(self, order: Order) -> Order: ...
    def cancel(self, order: Order) -> Order: ...


class ExchangeStateReader(Protocol):
    """Read-only view of exchange truth used by reconciliation & restart recovery."""

    def open_orders(self) -> list[Order]: ...
    def order_by_client_id(self, client_order_id: str) -> Order | None: ...
    def executions_since(self, since: datetime) -> list[Fill]: ...
    def positions(self) -> list[Position]: ...
