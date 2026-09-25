from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.domain.enums import EventSource


@dataclass(frozen=True)
class AccountSnapshot:
    """Point-in-time balance view. Exchange snapshots and locally derived ones are
    stored separately (source) so drift can be detected."""
    snapshot_id: str
    taken_at: datetime
    source: EventSource
    equity: Decimal
    wallet_balance: Decimal
    available_balance: Decimal
    unrealized_pnl: Decimal
    asset: str = "USDT"
