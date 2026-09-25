"""Pagination, backfill, idempotency, warmup (all offline)."""
import unittest
from datetime import timedelta
from decimal import Decimal as D

from app.data.backfill import plan_history
from app.data.errors import InsufficientHistory, PaginationError
from app.domain.enums import Category
from app.domain.market import Symbol, Timeframe
from tests.fakes import (T0, ScriptedTransport, SyntheticBybit, TempDB, make_provider, make_service, resp)

BTC = Symbol("BTCUSDT")
M15, H1, M1 = Timeframe.M15, Timeframe.H1, Timeframe.M1
NOW = T0 + timedelta(days=40, minutes=7)   # inside a forming 15m bar


class PaginationTests(unittest.TestCase):  # Test 5
    def test_static_pages_merge_with_boundary_overlap(self):
        t = ScriptedTransport(resp("kline_page1.json"), resp("kline_page2.json"))
        batch = make_provider(t, page_limit=3).candles(BTC, M15, T0, T0 + M15.delta * 5)
        self.assertEqual(batch.pages, 2)
        self.assertEqual([c.open_time for c in batch.candles], [T0 + M15.delta * i for i in range(5)])
        self.assertEqual(batch.duplicates_in_response, 1)       # bar 2 on both pages
        self.assertEqual(t.calls[1][1]["end"], str(1767225600000 + 2 * 900000 - 1))  # walked backwards

    def test_synthetic_multi_page(self):
        syn = SyntheticBybit(NOW, T0)
        start, end = T0, T0 + M15.delta * 2500
        batch = make_provider(syn, page_limit=1000, now=NOW).candles(BTC, M15, start, end)
        self.assertEqual(len(batch.candles), 2500)
        self.assertEqual(syn.kline_requests(), 3)
        ts = [c.open_time for c in batch.candles]
        self.assertEqual(ts, sorted(set(ts)))
        self.assertEqual(ts[0], start)
        self.assertEqual(ts[-1], end - M15.delta)

    def test_stops_at_listing(self):
        listing = T0 + timedelta(days=30)
        syn = SyntheticBybit(NOW, listing)
        batch = make_provider(syn, page_limit=200, now=NOW).candles(BTC, H1, T0, T0 + timedelta(days=35))
        self.assertEqual(len(batch.candles), 5 * 24)
        self.assertLessEqual(syn.kline_requests(), 2)

    def test_no_progress_raises(self):
        page = resp("kline_page1.json")
        t = ScriptedTransport(page, page, page)
        with self.assertRaises(PaginationError):
            make_provider(t, page_limit=3).candles(BTC, M15, T0 - timedelta(days=1), T0 + M15.delta * 5)

    def test_page_budget_enforced(self):
        class Crawling(SyntheticBybit):
            """Claims full pages but advances only 2 bars per page (padded with repeats)."""
            def get(self, url, params, timeout_s):
                import json as _j
                r = super().get(url, dict(params, limit="2"), timeout_s)
                doc = _j.loads(r.body)
                rows = doc["result"]["list"]
                doc["result"]["list"] = [rows[0]] * (int(params["limit"]) - len(rows)) + rows
                from app.data.http import HttpResponse
                return HttpResponse(200, _j.dumps(doc).encode(), {}, url)
        syn = Crawling(NOW, T0 - timedelta(days=30))
        with self.assertRaises(PaginationError):
            make_provider(syn, page_limit=10, now=NOW).candles(BTC, H1, T0, T0 + timedelta(hours=30))


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.db = TempDB()

    def tearDown(self):
        self.db.close()

    def test_end_to_end_ok(self):
        svc = make_service(SyntheticBybit(NOW, T0), self.db, now=NOW)
        r = svc.backfill(BTC, M15, T0, T0 + timedelta(days=2))
        self.assertEqual((r.status, r.inserted, r.gaps, r.invalid), ("ok", 192, (), 0))
        stored = self.db.store.get_candles(BTC, M15, T0, T0 + timedelta(days=2))
        self.assertEqual(len(stored), 192)
        self.assertEqual(self.db.store.runs()[0]["status"], "ok")

    def test_open_candle_never_persisted(self):
        syn = SyntheticBybit(NOW, T0)
        svc = make_service(syn, self.db, now=NOW)
        r = svc.backfill(BTC, M15, NOW - timedelta(hours=2), NOW + timedelta(hours=5))
        forming = M15.floor(NOW)
        self.assertEqual(r.end, forming)                   # clamped to closed bars
        self.assertEqual(self.db.store.last_candles(BTC, M15, NOW + timedelta(days=1), 1)[0].close_time, forming)

    def test_open_candle_from_exchange_excluded(self):
        # local clock lags the exchange: range reaches the bar Bybit still marks as forming
        svc = make_service(ScriptedTransport(resp("kline_with_open.json")), self.db,
                           now=T0 + timedelta(hours=2))
        r = svc.backfill(BTC, M15, T0, T0 + M15.delta * 5)
        self.assertEqual((r.open_excluded, r.inserted), (1, 4))
        from app.core.errors import StorageError
        from app.domain.market import Candle
        with self.assertRaises(StorageError):
            self.db.store.upsert_candles([Candle(BTC, M15, T0, D(1), D(1), D(1), D(1), D(0), is_closed=False)], "t")

    def test_invalid_records_make_status_not_ok(self):
        bad = {1767225600000 + 900000 * 10}
        svc = make_service(SyntheticBybit(NOW, T0, bad_value_at=bad), self.db, now=NOW)
        r = svc.backfill(BTC, M15, T0, T0 + timedelta(hours=5))
        self.assertEqual(r.status, "completed_with_issues")
        self.assertEqual((r.invalid, r.inserted, len(r.gaps)), (1, 19, 1))
        issues = self.db.store.issues(r.run_id)
        self.assertTrue(any(i["kind"] == "invalid_record" for i in issues))
        self.assertTrue(any(i["kind"] == "gap" for i in issues))

    def test_overlapping_ranges(self):
        svc = make_service(SyntheticBybit(NOW, T0), self.db, now=NOW)
        svc.backfill(BTC, H1, T0, T0 + timedelta(hours=100))
        r = svc.backfill(BTC, H1, T0 + timedelta(hours=50), T0 + timedelta(hours=150))
        self.assertEqual((r.inserted, r.unchanged), (50, 50))
        self.assertEqual(self.db.store.candle_count(BTC, H1), 150)

    def test_instruments_sync_and_missing_symbol(self):
        svc = make_service(SyntheticBybit(NOW, T0), self.db, now=NOW)
        self.assertEqual(svc.sync_instruments(Category.LINEAR, ["BTCUSDT"]), 1)
        self.assertEqual(self.db.store.instrument(BTC).tick_size, D("0.10"))
        with self.assertRaises(LookupError):
            svc.sync_instruments(Category.LINEAR, ["NOPEUSDT"])

    def test_instruments_cursor_pagination(self):
        t = ScriptedTransport(resp("instruments_linear_page1.json"), resp("instruments_linear_page2.json"))
        items = make_provider(t).instruments(Category.LINEAR)
        self.assertEqual([i.symbol.name for i in items], ["BTCUSDT", "ETHUSDT", "NEWUSDT"])
        self.assertEqual(t.calls[1][1]["cursor"], "first%3DETHUSDT")

    def test_funding_backfill(self):
        svc = make_service(ScriptedTransport(resp("funding_history.json")), self.db, now=NOW)
        run = svc.backfill_funding(BTC, T0 - timedelta(days=2), T0 + timedelta(hours=1))
        self.assertEqual((run.status, run.inserted), ("ok", 3))
        self.assertEqual(len(self.db.store.funding_rates(BTC, T0 - timedelta(days=2), NOW)), 3)


class IdempotencyTests(unittest.TestCase):  # Test 6
    def setUp(self):
        self.db = TempDB()

    def tearDown(self):
        self.db.close()

    def test_backfill_twice_same_count(self):
        syn = SyntheticBybit(NOW, T0)
        svc = make_service(syn, self.db, now=NOW)
        r1 = svc.backfill(BTC, M15, T0, T0 + timedelta(days=3))
        x = self.db.store.candle_count(BTC, M15)
        snapshot = [(c.open_time, c.close) for c in self.db.store.get_candles(BTC, M15, T0, NOW)]
        r2 = svc.backfill(BTC, M15, T0, T0 + timedelta(days=3))
        self.assertEqual(self.db.store.candle_count(BTC, M15), x)
        self.assertEqual((r1.inserted, r2.inserted, r2.unchanged), (x, 0, x))
        self.assertEqual(snapshot, [(c.open_time, c.close) for c in self.db.store.get_candles(BTC, M15, T0, NOW)])

    def test_changed_exchange_data_does_not_overwrite(self):
        make_service(SyntheticBybit(NOW, T0), self.db, now=NOW).backfill(BTC, H1, T0, T0 + timedelta(hours=10))
        before = self.db.store.get_candles(BTC, H1, T0, NOW)
        svc = make_service(SyntheticBybit(NOW, T0, price_shift=D(1)), self.db, now=NOW)
        r = svc.backfill(BTC, H1, T0, T0 + timedelta(hours=10))
        self.assertEqual((r.status, r.conflicts, r.inserted), ("completed_with_issues", 10, 0))
        self.assertEqual(self.db.store.get_candles(BTC, H1, T0, NOW), before)   # stored kept
        self.assertTrue(any(i["kind"] == "stored_conflict" for i in self.db.store.issues(r.run_id)))

    def test_restart_keeps_data(self):
        make_service(SyntheticBybit(NOW, T0), self.db, now=NOW).backfill(BTC, H1, T0, T0 + timedelta(hours=10))
        self.db.reopen()
        self.assertEqual(self.db.store.candle_count(BTC, H1), 10)
        r = make_service(SyntheticBybit(NOW, T0), self.db, now=NOW).backfill(BTC, H1, T0, T0 + timedelta(hours=10))
        self.assertEqual(r.inserted, 0)


class WarmupTests(unittest.TestCase):  # Test 8
    """History length comes from the feature requirements (single source of truth)."""

    def test_plan_depends_on_task(self):
        end = T0 + timedelta(days=30)
        p0 = plan_history(M15, 10, 1, end)                    # raw bars, no indicator
        self.assertEqual((p0.warmup_bars, p0.total_bars), (0, 10))
        p = plan_history(M15, 96, 800, end)                   # EMA-200 = 200 seed + 600 warmup
        self.assertEqual((p.usable_bars, p.warmup_bars, p.total_bars), (96, 799, 895))
        self.assertGreaterEqual(p.warmup_bars, 600)
        self.assertEqual(p.end - p.start, M15.delta * 895)
        with self.assertRaises(ValueError):
            plan_history(M15, 1, 0, end)

    def test_ensure_history_ema200(self):
        from app.research.service import FeatureService
        db = TempDB()
        try:
            per_value = FeatureService(db.store).bars_per_value(["ema_200"])
            self.assertGreaterEqual(per_value, 800)
            svc = make_service(SyntheticBybit(NOW, T0), db, now=NOW, page_limit=200)
            plan, report, candles = svc.ensure_history(BTC, M15, usable_bars=100, bars_per_value=per_value)
            self.assertEqual(len(candles), plan.total_bars)
            self.assertGreaterEqual(plan.warmup_bars, 600)
            self.assertEqual(candles[-1].close_time, M15.floor(NOW))
            self.assertGreater(report.pages, 3)            # needed several pages
        finally:
            db.close()

    def test_planned_history_is_enough_for_every_usable_point(self):
        """Regression: the Phase 1 lookback-based plan (600 + usable) left EMA-200
        (800 bars/value) INSUFFICIENT at every point while backfill reported ok."""
        from app.domain.enums import FeatureStatus
        from app.research.engine import FeatureEngine
        from app.research.service import FeatureService
        db = TempDB()
        try:
            names = ["ema_200", "atr_14", "ret_std_20"]
            per_value = FeatureService(db.store).bars_per_value(names)
            svc = make_service(SyntheticBybit(NOW, T0), db, now=NOW, page_limit=1000)
            plan, _, candles = svc.ensure_history(BTC, H1, usable_bars=50, bars_per_value=per_value)
            points = [c.close_time for c in candles[-50:]]
            cov = FeatureEngine().coverage(candles, H1, names, points)
            for n in names:
                self.assertEqual(cov.coverage_pct(n), 100.0, cov.explain(n))
            # the old plan would have been short:
            old_total = 50 + 600
            self.assertLess(old_total, per_value)
            self.assertIs(FeatureEngine().snapshot(candles[-old_total:], H1, points[-1], ["ema_200"])
                          .features["ema_200"].status, FeatureStatus.INSUFFICIENT_HISTORY)
        finally:
            db.close()

    def test_insufficient_history_raises(self):
        db = TempDB()
        try:
            recent = NOW - timedelta(hours=24)     # listed 1 day ago -> 96 bars only
            svc = make_service(SyntheticBybit(NOW, recent), db, now=NOW)
            with self.assertRaises(InsufficientHistory):
                svc.ensure_history(BTC, M15, usable_bars=50, bars_per_value=801)
        finally:
            db.close()
