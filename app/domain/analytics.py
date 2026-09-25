"""Analytics value objects. Computed from CLOSED trades only."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class TradeMetrics:
    trades: int
    wins: int
    losses: int
    net_pnl: Decimal
    fees: Decimal
    funding: Decimal
    avg_r: Decimal | None
    expectancy_r: Decimal | None


@dataclass(frozen=True)
class Drawdown:
    peak_equity: Decimal
    trough_equity: Decimal
    max_drawdown_pct: Decimal


@dataclass(frozen=True)
class ExecutionMetrics:
    fills: int
    maker_ratio: Decimal | None
    avg_slippage_bps: Decimal | None


@dataclass(frozen=True)
class PerformanceSnapshot:
    as_of: datetime
    metrics: TradeMetrics
    drawdown: Drawdown | None
    execution: ExecutionMetrics | None
