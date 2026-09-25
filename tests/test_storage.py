import shutil
import sqlite3
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from app.core.errors import StorageError
from app.domain.enums import ExitReason, FillRole, OrderStatus, OrderType, Side, TradeStatus
from app.domain.execution import Order
from app.storage.database import MIGRATIONS_DIR, bootstrap, connect, migrate
from app.storage.repositories import EventLog, FillRepository, OrderRepository, TradeRepository
from tests.factories import BTC, fill, trade, ts


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="qre_test_"))
        self.db = self.dir / "t.sqlite3"
        self.conn = bootstrap(self.db)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_bootstrap_creates_schema(self):
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("orders", "fills", "trades", "trade_legs", "funding_events", "position_events",
                  "order_events", "exchange_snapshots", "event_log", "schema_migrations"):
            self.assertIn(t, tables)
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_migrations_idempotent(self):
        self.assertEqual(migrate(self.conn), [])

    def test_modified_migration_detected(self):
        mdir = self.dir / "m"
        shutil.copytree(MIGRATIONS_DIR, mdir)
        c = connect(self.dir / "x.sqlite3")
        migrate(c, mdir)
        f = sorted(mdir.glob("*.sql"))[0]
        f.write_text(f.read_text() + "\n-- edit\nCREATE TABLE x(a);\n")
        with self.assertRaises(StorageError):
            migrate(c, mdir)
        c.close()

    def test_fill_dedup_and_append_only(self):
        repo = FillRepository(self.conn)
        f = fill("e1", Side.BUY, "1", "100", order=None)
        self.assertTrue(repo.add(f))
        self.assertFalse(repo.add(f))
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE fills SET price='1' WHERE exec_id='e1'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("DELETE FROM fills WHERE exec_id='e1'")

    def test_initial_stop_immutable_in_db(self):
        TradeRepository(self.conn).create(trade())
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE trades SET initial_stop='95' WHERE trade_id='trd_1'")

    def test_fill_belongs_to_one_trade(self):
        tr = TradeRepository(self.conn)
        tr.create(trade())
        FillRepository(self.conn).add(fill("e1", Side.BUY, "1", "100", order=None))
        self.assertTrue(tr.link_fill("trd_1", "e1", FillRole.ENTRY))
        self.assertFalse(tr.link_fill("trd_1", "e1", FillRole.ENTRY))

    def test_event_log_idempotent(self):
        log = EventLog(self.conn)
        self.assertTrue(log.append("k1", "order_accepted", "ord_1", ts(), {"a": 1}))
        self.assertFalse(log.append("k1", "order_accepted", "ord_1", ts(), {"a": 1}))
        self.assertEqual(log.count("ord_1"), 1)

    def test_restart_recovery_rebuilds_state(self):
        """Process dies after fills are persisted; a new process must rebuild the
        same trade and find the non-terminal order to reconcile."""
        orders, fills, trades = OrderRepository(self.conn), FillRepository(self.conn), TradeRepository(self.conn)
        trades.create(trade())
        o = Order("ord_1", "qlink1", BTC, Side.BUY, OrderType.LIMIT, D("1"), ts(), ts(), price=D("100"),
                  trade_id="trd_1")
        orders.save(o)
        o = o.transition(OrderStatus.SUBMITTING, ts(1)).transition(OrderStatus.ACCEPTED, ts(2), exchange_order_id="ex1")
        orders.save(o)
        for f, role in [(fill("e1", Side.BUY, "0.4", "100", "0.02", 3), FillRole.ENTRY),
                        (fill("e2", Side.BUY, "0.6", "100", "0.03", 4), FillRole.ENTRY)]:
            fills.add(f)
            trades.link_fill("trd_1", f.exec_id, role)
        o = o.transition(OrderStatus.FILLED, ts(4))
        orders.save(o)
        orders.save(Order("ord_2", "qlink2", BTC, Side.SELL, OrderType.LIMIT, D("1"), ts(5), ts(5),
                          price=D("110"), reduce_only=True, trade_id="trd_1")
                    .transition(OrderStatus.SUBMITTING, ts(6)).transition(OrderStatus.UNKNOWN, ts(7)))
        fills.add(fill("x1", Side.SELL, "1", "110", "0.05", 8, order=None))  # exchange-reported
        trades.link_fill("trd_1", "x1", FillRole.EXIT)
        trades.set_exit_reason("trd_1", ExitReason.TAKE_PROFIT)
        self.conn.close()

        # ---- "restart"
        self.conn = bootstrap(self.db)
        t = TradeRepository(self.conn).load("trd_1")
        self.assertEqual(t.status, TradeStatus.CLOSED)
        self.assertEqual(t.pnl().net, D("9.90"))
        self.assertEqual(t.exit_reason, ExitReason.TAKE_PROFIT)
        pending = OrderRepository(self.conn).non_terminal()
        self.assertEqual([p.local_order_id for p in pending], ["ord_2"])
        self.assertEqual(pending[0].status, OrderStatus.UNKNOWN)
        self.assertEqual(OrderRepository(self.conn).get("ord_1").exchange_order_id, "ex1")
