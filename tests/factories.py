"""Deterministic builders for tests. No network, no real keys."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from app.domain.accounting import Trade
from app.domain.decision import RiskPlan
from app.domain.enums import EventSource, PositionSide, Side
from app.domain.execution import Fill
from app.domain.market import Symbol

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
BTC = Symbol("BTCUSDT")


def ts(sec: int = 0) -> datetime:
    return T0 + timedelta(seconds=sec)


def risk_plan(side=PositionSide.LONG, entry="100", stop="90", qty="1") -> RiskPlan:
    return RiskPlan("rsk_1", "set_1", side, D(entry), D(stop), D(qty))


def trade(side=PositionSide.LONG, **kw) -> Trade:
    return Trade("trd_1", BTC, side, risk_plan(side, **kw))


def fill(exec_id: str, side: Side, qty: str, price: str, fee: str = "0", sec: int = 0,
         expected: str | None = None, order: str | None = "ord_1") -> Fill:
    return Fill(exec_id=exec_id, symbol=BTC, side=side, qty=D(qty), price=D(price), fee=D(fee),
                fee_asset="USDT", is_maker=False, exec_time=ts(sec), source=EventSource.SIMULATOR,
                local_order_id=order, expected_price=None if expected is None else D(expected))
