"""Repositories translate domain objects <-> rows. Idempotent writes."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from app.domain.accounting import FundingEvent, Trade
from app.domain.decision import RiskPlan
from app.domain.enums import (
    Category, EventSource, ExitReason, FillRole, OrderStatus, OrderType, PositionSide,
    ReconciliationStatus, Side, TimeInForce,
)
from app.domain.execution import Fill, Order
from app.domain.market import Symbol


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _d(v: str | None) -> Decimal | None:
    return None if v is None else Decimal(v)


class OrderRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def save(self, o: Order) -> None:
        self.conn.execute(
            """INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(local_order_id) DO UPDATE SET
                 exchange_order_id=excluded.exchange_order_id, status=excluded.status,
                 reconciliation_status=excluded.reconciliation_status,
                 updated_at=excluded.updated_at""",
            (o.local_order_id, o.client_order_id, o.exchange_order_id, o.trade_id, o.signal_id,
             o.symbol.name, o.symbol.category.value, o.side.value, o.order_type.value,
             o.time_in_force.value, str(o.qty), None if o.price is None else str(o.price),
             int(o.reduce_only), o.status.value, o.source.value, o.reconciliation_status.value,
             o.created_at.isoformat(), o.updated_at.isoformat()),
        )

    def _row(self, r: sqlite3.Row) -> Order:
        return Order(
            local_order_id=r["local_order_id"], client_order_id=r["client_order_id"],
            symbol=Symbol(r["symbol"], Category(r["category"])), side=Side(r["side"]),
            order_type=OrderType(r["order_type"]), qty=Decimal(r["qty"]),
            created_at=_ts(r["created_at"]), updated_at=_ts(r["updated_at"]),
            price=_d(r["price"]), time_in_force=TimeInForce(r["time_in_force"]),
            reduce_only=bool(r["reduce_only"]), trade_id=r["trade_id"], signal_id=r["signal_id"],
            exchange_order_id=r["exchange_order_id"], status=OrderStatus(r["status"]),
            source=EventSource(r["source"]),
            reconciliation_status=ReconciliationStatus(r["reconciliation_status"]),
        )

    def get(self, local_order_id: str) -> Order | None:
        r = self.conn.execute("SELECT * FROM orders WHERE local_order_id=?", (local_order_id,)).fetchone()
        return self._row(r) if r else None

    def non_terminal(self) -> list[Order]:
        """Orders that must be reconciled after a restart."""
        terminal = [s.value for s in OrderStatus if s.is_terminal]
        q = f"SELECT * FROM orders WHERE status NOT IN ({','.join('?' * len(terminal))})"
        return [self._row(r) for r in self.conn.execute(q, terminal)]


class FillRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add(self, f: Fill) -> bool:
        """Returns True if inserted, False if this exec_id was already known."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f.exec_id, f.local_order_id, f.exchange_order_id, f.symbol.name,
             f.symbol.category.value, f.side.value, str(f.qty), str(f.price), str(f.fee),
             f.fee_asset, int(f.is_maker),
             None if f.expected_price is None else str(f.expected_price),
             f.exec_time.isoformat(), f.source.value, _now()),
        )
        return cur.rowcount == 1

    def _row(self, r: sqlite3.Row) -> Fill:
        return Fill(
            exec_id=r["exec_id"], symbol=Symbol(r["symbol"], Category(r["category"])),
            side=Side(r["side"]), qty=Decimal(r["qty"]), price=Decimal(r["price"]),
            fee=Decimal(r["fee"]), fee_asset=r["fee_asset"], is_maker=bool(r["is_maker"]),
            exec_time=_ts(r["exec_time"]), source=EventSource(r["source"]),
            local_order_id=r["local_order_id"], exchange_order_id=r["exchange_order_id"],
            expected_price=_d(r["expected_price"]),
        )

    def for_order(self, local_order_id: str) -> list[Fill]:
        return [self._row(r) for r in self.conn.execute(
            "SELECT * FROM fills WHERE local_order_id=? ORDER BY exec_time, exec_id", (local_order_id,))]


class TradeRepository:
    """Stores the trade header and leg links. Loading replays legs -> Trade."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.fills = FillRepository(conn)

    def create(self, t: Trade) -> None:
        rp = t.risk_plan
        self.conn.execute(
            "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (t.trade_id, t.symbol.name, t.symbol.category.value, t.side.value, t.signal_id,
             rp.risk_plan_id, str(rp.planned_entry), str(rp.initial_stop), str(rp.planned_qty),
             None, _now(), ReconciliationStatus.PENDING.value),
        )

    def link_fill(self, trade_id: str, exec_id: str, role: FillRole) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO trade_legs VALUES (?,?,?)", (trade_id, exec_id, role.value))
        return cur.rowcount == 1

    def add_funding(self, ev: FundingEvent) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO funding_events VALUES (?,?,?,?,?,?,?,?)",
            (ev.funding_id, ev.exchange_ref, ev.trade_id, ev.symbol.name, str(ev.amount),
             ev.asset, None if ev.rate is None else str(ev.rate), ev.occurred_at.isoformat()))
        return cur.rowcount == 1

    def set_exit_reason(self, trade_id: str, reason: ExitReason) -> None:
        self.conn.execute("UPDATE trades SET exit_reason=? WHERE trade_id=? AND exit_reason IS NULL",
                          (reason.value, trade_id))

    def load(self, trade_id: str) -> Trade | None:
        h = self.conn.execute("SELECT * FROM trades WHERE trade_id=?", (trade_id,)).fetchone()
        if not h:
            return None
        symbol = Symbol(h["symbol"], Category(h["category"]))
        side = PositionSide(h["side"])
        rp = RiskPlan(h["risk_plan_id"], "loaded", side, Decimal(h["planned_entry"]),
                      Decimal(h["initial_stop"]), Decimal(h["planned_qty"]))
        trade = Trade(h["trade_id"], symbol, side, rp, signal_id=h["signal_id"])
        rows = self.conn.execute(
            "SELECT f.*, l.role FROM trade_legs l JOIN fills f ON f.exec_id=l.exec_id "
            "WHERE l.trade_id=? ORDER BY f.exec_time, f.exec_id", (trade_id,))
        for r in rows:
            trade = trade.apply_fill(self.fills._row(r), FillRole(r["role"]))
        for r in self.conn.execute("SELECT * FROM funding_events WHERE trade_id=?", (trade_id,)):
            trade = trade.apply_funding(FundingEvent(
                r["funding_id"], symbol, Decimal(r["amount"]), r["asset"], _ts(r["occurred_at"]),
                r["exchange_ref"], _d(r["rate"]), r["trade_id"]))
        if h["exit_reason"]:
            trade = trade.close_with_reason(ExitReason(h["exit_reason"]))
        return trade


class EventLog:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def append(self, key: str, event_type: str, aggregate_id: str, occurred_at: datetime,
               payload: dict, correlation_id: str | None = None) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO event_log(idempotency_key, event_type, aggregate_id, "
            "correlation_id, occurred_at, recorded_at, payload_json) VALUES (?,?,?,?,?,?,?)",
            (key, event_type, aggregate_id, correlation_id, occurred_at.isoformat(), _now(),
             json.dumps(payload, sort_keys=True, default=str)))
        return cur.rowcount == 1

    def count(self, aggregate_id: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM event_log WHERE aggregate_id=?", (aggregate_id,)).fetchone()[0]
