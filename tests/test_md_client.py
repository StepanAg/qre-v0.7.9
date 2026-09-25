"""Failure handling, retry/backoff, rate limits. Offline (scripted transport)."""
import random
import unittest
from datetime import timedelta

from app.data.errors import (BybitApiError, ConnectionFailed, HttpStatusError, MalformedResponse,
                             NetworkTimeout, RetriesExhausted)
from app.data.http import HttpResponse, RequestPacer, RetryPolicy
from app.domain.market import Symbol, Timeframe
from tests.fakes import T0, ScriptedTransport, SleepRecorder, TempDB, make_executor, make_provider, make_service, resp

BTC = Symbol("BTCUSDT")
M15 = Timeframe.M15
RANGE = (T0, T0 + M15.delta * 5)


class FailureHandlingTests(unittest.TestCase):  # Test 9 and friends
    def test_api_error_is_raised_not_empty(self):
        t = ScriptedTransport(resp("api_error_10001.json"))
        with self.assertRaises(BybitApiError) as cm:
            make_provider(t).candles(BTC, M15, *RANGE)
        self.assertEqual(cm.exception.ret_code, 10001)
        self.assertEqual(len(t.calls), 1)          # non-retryable -> exactly one request

    def test_http_404_not_retried(self):
        t = ScriptedTransport(HttpResponse(404, b"not found", {}, "u"))
        with self.assertRaises(HttpStatusError):
            make_provider(t).candles(BTC, M15, *RANGE)
        self.assertEqual(len(t.calls), 1)

    def test_malformed_json_not_retried(self):
        t = ScriptedTransport(resp("malformed.txt"))
        with self.assertRaises(MalformedResponse):
            make_provider(t).candles(BTC, M15, *RANGE)
        self.assertEqual(len(t.calls), 1)

    def test_timeout_and_connection_errors_retried_then_raised(self):
        for exc in (NetworkTimeout("t"), ConnectionFailed("c")):
            with self.subTest(exc=type(exc).__name__):
                t = ScriptedTransport(exc, exc, exc)
                with self.assertRaises(RetriesExhausted) as cm:
                    make_provider(t, attempts=3).candles(BTC, M15, *RANGE)
                self.assertIs(type(cm.exception.last), type(exc))
                self.assertEqual(len(t.calls), 3)

    def test_transient_5xx_then_success(self):
        t = ScriptedTransport(HttpResponse(502, b"bad gateway", {}, "u"), resp("kline_normal.json"))
        batch = make_provider(t).candles(BTC, M15, *RANGE)
        self.assertEqual(len(batch.candles), 5)
        self.assertEqual(len(t.calls), 2)

    def test_backfill_failure_is_visible_and_recorded(self):
        db = TempDB()
        try:
            svc = make_service(ScriptedTransport(resp("api_error_10001.json")), db, now=T0 + timedelta(days=1))
            with self.assertRaises(BybitApiError):
                svc.backfill(BTC, M15, *RANGE)
            runs = db.store.runs()
            self.assertEqual(runs[0]["status"], "failed")
            self.assertIn("10001", runs[0]["error"])
            self.assertEqual(db.store.candle_count(BTC, M15), 0)
        finally:
            db.close()


class RateLimitRetryTests(unittest.TestCase):
    def test_rate_limit_retcode_retried_with_backoff(self):
        sleep = SleepRecorder()
        t = ScriptedTransport(resp("api_error_10006.json"), resp("api_error_10006.json"), resp("kline_normal.json"))
        p = make_provider(t, sleep=sleep)
        p.candles(BTC, M15, *RANGE)
        self.assertEqual(p.metrics.retries, 2)
        self.assertEqual(p.metrics.rate_limited, 2)
        self.assertEqual(len(sleep.calls), 2)
        self.assertLess(sleep.calls[0], sleep.calls[1])  # exponential

    def test_retries_are_bounded(self):
        t = ScriptedTransport(*[resp("api_error_10006.json")] * 4)
        with self.assertRaises(RetriesExhausted) as cm:
            make_provider(t, attempts=4).candles(BTC, M15, *RANGE)
        self.assertEqual(cm.exception.attempts, 4)
        self.assertIsInstance(cm.exception.last, BybitApiError)

    def test_http_429_honours_reset_header(self):
        import time
        sleep = SleepRecorder()
        reset = str(int((time.time() + 5) * 1000))
        t = ScriptedTransport(HttpResponse(429, b"", {"X-Bapi-Limit-Reset-Timestamp": reset}, "u"),
                              resp("kline_normal.json"))
        make_provider(t, sleep=sleep).candles(BTC, M15, *RANGE)
        self.assertGreaterEqual(sleep.calls[0], 3.5)

    def test_403_reported_with_geo_hint(self):
        t = ScriptedTransport(*[HttpResponse(403, b"forbidden", {}, "u")] * 2)
        with self.assertRaises(RetriesExhausted) as cm:
            make_provider(t, attempts=2).candles(BTC, M15, *RANGE)
        self.assertIn("geo", str(cm.exception.last))

    def test_backoff_policy_bounds(self):
        p = RetryPolicy(base_delay_s=0.5, max_delay_s=4, jitter=0)
        rng = random.Random(0)
        self.assertEqual([p.delay(a, rng) for a in range(1, 6)], [0.5, 1, 2, 4, 4])

    def test_pacer_spaces_requests(self):
        clock = [100.0]
        sleep = SleepRecorder()
        pacer = RequestPacer(0.2, monotonic=lambda: clock[0], sleep=sleep)
        pacer.wait()
        clock[0] += 0.05
        pacer.wait()
        self.assertEqual(len(sleep.calls), 1)
        self.assertAlmostEqual(sleep.calls[0], 0.15)

    def test_metrics_count_requests_and_errors(self):
        t = ScriptedTransport(resp("api_error_10006.json"), resp("kline_normal.json"))
        ex = make_executor(t)
        from app.data.bybit.client import BybitRestClient
        BybitRestClient("https://api.bybit.com", ex).get("/v5/market/kline", {})
        self.assertEqual((ex.metrics.api_requests, ex.metrics.api_errors, ex.metrics.retries), (2, 1, 1))
        self.assertEqual(ex.metrics.error_kinds, {"BybitApiError": 1})
