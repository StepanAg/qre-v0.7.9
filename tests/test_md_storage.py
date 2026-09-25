"""SQLite persistence, migrations, single writer, DB failure visibility."""
import shutil
import sqlite3
import tempfile
import threading
import unittest
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

from app.core.errors import StorageError
from app.domain.market import Candle, Symbol, Timeframe
from app.storage.database import MIGRATIONS_DIR, bootstrap, connect, migrate
from app.storage.market_data import SQLiteMarketDataStore
from app.storage.writer import SerializedWriter
from tests.fakes import T0, SyntheticBybit, TempDB, make_service

BTC, ETH = Symbol("BTCUSDT"), Symbol("ETHUSDT")
M15 = Timeframe.M15
NOW = T0 + timedelta(days=5)


def c(i, sym=BTC):
    return Candle(sym, M15, T0 + M15.delta * i, D("87000.10"), D("87100.00"), D("86900.5"), D("87050.25"),
                  D("12.345"), D("1074553.21"))


class SQLitePersistenceTests(unittest.TestCase):  # Test 12
    def setUp(self):
        self.db = TempDB()

    def tearDown(self):
        self.db.close()

    def test_schema_created(self):
        tables = {r[0] for r in self.db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"instruments", "candles", "funding_rates", "backfill_runs",
                         "data_quality_issues"} <= tables)
        self.assertNotIn("tickers", tables)   # tickers are live snapshots, not persisted (documented)

    def test_roundtrip_exact_decimals(self):
        self.db.store.upsert_candles([c(0), c(1)], "t")
        got = self.db.store.get_candles(BTC, M15, T0, T0 + timedelta(hours=1))
        self.assertEqual(got, [c(0), c(1)])
        self.assertEqual(got[0].turnover, D("1074553.21"))

    def test_symbols_and_timeframes_isolated(self):
        self.db.store.upsert_candles([c(0), c(0, ETH)], "t")
        self.assertEqual(self.db.store.candle_count(BTC, M15), 1)
        self.assertEqual(self.db.store.candle_count(BTC, Timeframe.H1), 0)

    def test_triggers_protect_data(self):
        self.db.store.upsert_candles([c(0)], "t")
        with self.assertRaises(sqlite3.DatabaseError):
            self.db.conn.execute("UPDATE candles SET close='1'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.db.conn.execute("INSERT INTO candles VALUES ('linear','BTCUSDT','15m',1767225660000,"
                                 "'1','1','1','1','0','0','t','x')")
        with self.assertRaises(sqlite3.DatabaseError):
            self.db.conn.execute("INSERT INTO candles VALUES ('linear','BTCUSDT','15',1767225600000,"
                                 "'1','1','1','1','0','0','t','x')")

    def test_db_failure_is_visible(self):
        self.db.conn.execute("DROP TABLE candles")
        svc = make_service(SyntheticBybit(NOW, T0), self.db, now=NOW)
        with self.assertRaises(StorageError):
            svc.backfill(BTC, M15, T0, T0 + timedelta(hours=2))
        with self.assertRaises(StorageError):
            self.db.store.get_candles(BTC, M15, T0, NOW)

    def test_last_candles_for_research(self):
        self.db.store.upsert_candles([c(i) for i in range(10)], "t")
        last = self.db.store.last_candles(BTC, M15, T0 + M15.delta * 8, 3)
        self.assertEqual([x.open_time for x in last], [T0 + M15.delta * i for i in (5, 6, 7)])


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="qre_mig_"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_all_migrations_applied_in_order(self):
        conn = bootstrap(self.dir / "a.sqlite3")
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
        all_files = sorted(p.stem for p in MIGRATIONS_DIR.glob("*.sql"))
        self.assertEqual(versions, all_files)                       # every migration applied, in order
        self.assertEqual(versions[:2], ["0001_initial", "0002_market_data"])
        self.assertEqual(migrate(conn), [])
        conn.close()

    def test_phase0_database_upgrades(self):
        only0 = self.dir / "m0"
        only0.mkdir()
        shutil.copy(MIGRATIONS_DIR / "0001_initial.sql", only0)
        conn = connect(self.dir / "old.sqlite3")
        migrate(conn, only0)
        conn.execute("INSERT INTO event_log(idempotency_key, event_type, aggregate_id, occurred_at, "
                     "recorded_at, payload_json) VALUES ('k','e','a','t','t','{}')")
        applied = migrate(conn)
        self.assertEqual(applied[0], "0002_market_data")            # later phases may add more
        self.assertEqual(applied, sorted(applied))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM event_log").fetchone()[0], 1)  # data kept
        conn.close()

    def test_single_migration_framework(self):
        files = sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
        self.assertEqual(files[:2], ["0001_initial.sql", "0002_market_data.sql"])
        self.assertEqual([f[:4] for f in files], [f"{i:04d}" for i in range(1, len(files) + 1)])  # no holes


class SingleWriterTests(unittest.TestCase):
    def test_concurrent_producers_serialised(self):
        d = Path(tempfile.mkdtemp(prefix="qre_w_"))
        try:
            conn = bootstrap(d / "w.sqlite3", shared=True)
            store = SQLiteMarketDataStore(SerializedWriter(conn))
            errors = []

            def producer(k):
                try:
                    for j in range(20):
                        store.upsert_candles([c(k * 100 + j)], f"p{k}")
                except Exception as e:  # collected and asserted below
                    errors.append(e)

            threads = [threading.Thread(target=producer, args=(k,)) for k in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            self.assertEqual(store.candle_count(BTC, M15), 160)
            conn.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_failed_write_rolls_back(self):
        db = TempDB()
        try:
            with self.assertRaises(StorageError):
                with db.writer.write() as conn:
                    conn.execute("INSERT INTO candles VALUES ('linear','BTCUSDT','15m',1767225600000,"
                                 "'1','1','1','1','0','0','t','x')")
                    conn.execute("INSERT INTO nope VALUES (1)")
            self.assertEqual(db.store.candle_count(BTC, M15), 0)
        finally:
            db.close()
