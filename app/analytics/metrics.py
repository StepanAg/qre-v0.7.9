from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from app.domain.accounting import Trade
from app.domain.analytics import TradeMetrics
from app.domain.enums import TradeStatus

ZERO = Decimal("0")


def trade_metrics(trades: Iterable[Trade]) -> TradeMetrics:
    """Open/planned trades are excluded: stats never count unfinished trades."""
    closed = [t for t in trades if t.status is TradeStatus.CLOSED]
    pnls = [t.pnl() for t in closed]
    rs = [r for r in (t.realized_r() for t in closed) if r is not None]
    avg_r = (sum(rs, ZERO) / len(rs)) if rs else None
    return TradeMetrics(
        trades=len(closed),
        wins=sum(1 for p in pnls if p.net > 0),
        losses=sum(1 for p in pnls if p.net <= 0),
        net_pnl=sum((p.net for p in pnls), ZERO),
        fees=sum((p.fees for p in pnls), ZERO),
        funding=sum((p.funding for p in pnls), ZERO),
        avg_r=avg_r,
        expectancy_r=avg_r,
    )
