"""Duplicate detection and gap detection."""
import sqlite3
import unittest
from datetime import timedelta
from decimal import Decimal as D

from app.data.quality import dedupe, find_gaps
from app.domain.market import Candle, Symbol, Timeframe
from tests.fakes import T0, ScriptedTransport, SyntheticBybit, TempDB, make_provider, make_service, resp

BTC = Symbol("BTCUSDT")
M15, H1 = Timeframe.M15, Timeframe.H1


def c(i, close="10", tf=M15):
    return Candle(BTC, tf, T0 + tf.delta * i, D(10), D(12), D(9), D(close), D(1), D(10))


class DuplicateDetectionTests(unittest.TestCase):  # Test 3
    def test_identical_duplicates_collapsed(self):
        out, dups, issues = dedupe([c(0), c(1), c(1), c(2)])
        self.assertEqual((len(out), dups, issues), (3, 1, []))

    def test_conflicting_duplicates_both_rejected(self):
        out, dups, issues = dedupe([c(0), c(1, "10"), c(1, "11"), c(1, "10"), c(2)])
        self.assertEqual([x.open_time for x in out], [T0, T0 + M15.delta * 2])
        self.assertEqual([i.kind for i in issues], ["conflicting_duplicate"])

    def test_duplicates_fixture_through_provider(self):
        batch = make_provider(ScriptedTransport(resp("kline_duplicates.json"))).candles(
            BTC, M15, T0, T0 + M15.delta * 5)
        self.assertEqual(batch.duplicates_in_response, 1)
        self.assertEqual(len(batch.candles), 4)      # conflicting bar 2 dropped
        self.assertIn("conflicting_duplicate", [i.kind for i in batch.issues])

    def test_db_insert_twice_no_duplicate(self):
        db = TempDB()
        try:
            r1 = db.store.upsert_candles([c(0), c(1)], "t")
            r2 = db.store.upsert_candles([c(0), c(1)], "t")
            self.assertEqual((r1.inserted, r2.inserted, r2.unchanged), (2, 0, 2))
            self.assertEqual(db.store.candle_count(BTC, M15), 2)
        finally:
            db.close()

    def test_uniqueness_enforced_by_database_not_python(self):
        db = TempDB()
        try:
            row = ("linear", "BTCUSDT", "15m", 1767225600000, "1", "1", "1", "1", "0", "0", "t", "x")
            db.conn.execute("INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", row)
            with self.assertRaises(sqlite3.IntegrityError):
                db.conn.execute("INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", row)
        finally:
            db.close()


class GapDetectionTests(unittest.TestCase):  # Test 4
    def test_missing_bar_detected(self):
        times = [T0 + timedelta(minutes=m) for m in (0, 15, 45)]
        (g,) = find_gaps(times, M15)
        self.assertEqual((g.start, g.end, g.missing_bars), (T0 + timedelta(minutes=30), T0 + timedelta(minutes=45), 1))

    def test_contiguous_no_gap(self):
        times = [T0 + timedelta(minutes=m) for m in (0, 15, 30, 45)]
        self.assertEqual(find_gaps(times, M15, T0, T0 + timedelta(hours=1)), [])

    def test_timeframe_aware(self):
        hourly = [T0 + timedelta(hours=h) for h in range(4)]
        self.assertEqual(find_gaps(hourly, H1), [])
        self.assertEqual(sum(g.missing_bars for g in find_gaps(hourly, M15)), 9)

    def test_head_and_tail_gaps(self):
        times = [T0 + timedelta(hours=1)]
        gaps = find_gaps(times, H1, T0, T0 + timedelta(hours=4))
        self.assertEqual([g.missing_bars for g in gaps], [1, 2])

    def test_before_listing_is_not_a_gap(self):
        listing = T0 + timedelta(hours=2)
        times = [T0 + timedelta(hours=h) for h in (2, 3)]
        self.assertEqual(find_gaps(times, H1, listing, T0 + timedelta(hours=4)), [])
        self.assertEqual(len(find_gaps(times, H1, T0, T0 + timedelta(hours=4))), 1)

    def test_unsorted_input_rejected(self):
        with self.assertRaises(ValueError):
            find_gaps([T0 + timedelta(hours=1), T0], H1)

    def test_gap_fixture_reported_by_backfill(self):
        db = TempDB()
        try:
            svc = make_service(ScriptedTransport(resp("kline_gap.json")), db, now=T0 + timedelta(days=1))
            r = svc.backfill(BTC, M15, T0, T0 + M15.delta * 5)
            self.assertEqual(r.status, "ok_with_gaps")
            self.assertEqual([(g.start, g.missing_bars) for g in r.gaps], [(T0 + timedelta(minutes=30), 1)])
            self.assertEqual(svc.metrics.gaps_detected, 1)
        finally:
            db.close()

    def test_listing_time_from_instruments_prevents_false_gap(self):
        now = T0 + timedelta(days=1)
        listing = T0 + timedelta(hours=10)
        db = TempDB()
        try:
            svc = make_service(SyntheticBybit(now, listing), db, now=now)
            svc.sync_instruments(__import__("app.domain.enums", fromlist=["Category"]).Category.LINEAR, ["BTCUSDT"])
            r = svc.backfill(BTC, M15, T0, T0 + timedelta(hours=20))
            self.assertEqual((r.status, r.inserted), ("ok", 40))
        finally:
            db.close()
