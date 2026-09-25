"""Rebuild trades from the persistent event store. This is what restart
recovery relies on: no PnL lives only in process memory."""
from __future__ import annotations

from app.domain.accounting import Trade
from app.domain.enums import FillRole
from app.domain.execution import Fill


def rebuild_trade(skeleton: Trade, fills_with_roles: list[tuple[Fill, FillRole]]) -> Trade:
    trade = skeleton
    for fill, role in fills_with_roles:
        trade = trade.apply_fill(fill, role)
    return trade
