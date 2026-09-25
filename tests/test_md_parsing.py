"""Parsing, normalisation, timestamps, candle invariants, fixtures, interfaces."""
import importlib
import inspect
import json
import pkgutil
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from app.core.clock import epoch_ms_to_utc, epoch_seconds_to_utc, normalize_timestamp, utc_to_epoch_ms
from app.data.bybit.client import ALLOWED_ENDPOINTS, BybitRestClient
from app.data.bybit.parser import (FROM_BYBIT_INTERVAL, TO_BYBIT_INTERVAL, parse_envelope, parse_funding,
                                   parse_instruments, parse_klines, parse_server_time, parse_tickers)
from app.data.bybit.provider import BybitMarketDataProvider
from app.data.errors import BybitApiError, MalformedResponse
from app.data.ports import MarketDataProvider, MarketDataSink, MarketDataStore
from app.domain.enums import Category
from app.domain.errors import DomainError
from app.domain.market import Candle, Symbol, Timeframe
from app.storage.market_data import SQLiteMarketDataStore
from tests.fakes import FIX, T0, T0_MS, fixture_bytes, make_provider, resp, ScriptedTransport

BTC = Symbol("BTCUSDT")
LATER = T0 + timedelta(days=1)
M15 = Timeframe.M15


def klines(name, now=LATER):
    result, _ = parse_envelope(fixture_bytes(name), "/v5/market/kline")
    return parse_klines(result, BTC, M15, now)


class MarketDataImportTests(unittest.TestCase):
    def test_all_data_modules_import(self):
        import app.data
        names = [m.name for m in pkgutil.walk_packages(app.data.__path__, "app.data.")]
        for n in names:
            importlib.import_module(n)
        for required in ("app.data.http", "app.data.ports", "app.data.quality", "app.data.backfill",
                         "app.data.bybit.client", "app.data.bybit.parser", "app.data.bybit.provider"):
            self.assertIn(required, names)

    def test_only_public_market_endpoints(self):
        self.assertTrue(all(e.startswith("/v5/market/") for e in ALLOWED_ENDPOINTS))
        client = BybitRestClient("https://api.bybit.com", None)  # executor unused before check
        for bad in ("/v5/order/create", "/v5/account/wallet-balance", "/v5/position/list"):
            with self.assertRaises(ValueError):
                client.get(bad, {})
        with self.assertRaises(ValueError):
            BybitRestClient("http://api.bybit.com", None)


class ProviderInterfaceTests(unittest.TestCase):
    def _implements(self, cls, proto):
        for name, member in inspect.getmembers(proto, inspect.isfunction):
            if name.startswith("_"):
                continue
            self.assertTrue(hasattr(cls, name), f"{cls.__name__} missing {name}")
            want = list(inspect.signature(member).parameters)
            have = list(inspect.signature(getattr(cls, name)).parameters)
            self.assertEqual(have[:len(want)], want, f"{cls.__name__}.{name} signature")

    def test_bybit_provider_implements_port(self):
        self._implements(BybitMarketDataProvider, MarketDataProvider)
        self.assertEqual(BybitMarketDataProvider.name, "bybit")

    def test_sqlite_store_implements_store_and_sink(self):
        self._implements(SQLiteMarketDataStore, MarketDataStore)
        self._implements(SQLiteMarketDataStore, MarketDataSink)

    def test_provider_is_replaceable(self):
        """BackfillService works with any object satisfying the port (no Bybit types)."""
        from app.core.clock import FixedClock
        from app.data.backfill import BackfillService
        from app.data.ports import CandleBatch, FundingBatch
        from tests.fakes import TempDB

        class OtherExchange:
            name = "other"
            def server_time(self): return LATER
            def instruments(self, category, symbols=None): return []
            def tickers(self, category, symbols=None): return []
            def funding_history(self, symbol, start, end): return FundingBatch(symbol, start, end, ())
            def candles(self, symbol, timeframe, start, end):
                cs = tuple(Candle(symbol, timeframe, start + timeframe.delta * i, D(10), D(11), D(9), D(10),
                                  D(1), D(10)) for i in range(4))
                return CandleBatch(symbol, timeframe, start, end, cs, pages=1)

        db = TempDB()
        try:
            svc = BackfillService(OtherExchange(), db.store, db.store, clock=FixedClock(LATER))
            r = svc.backfill(BTC, M15, T0, T0 + M15.delta * 4)
            self.assertEqual((r.inserted, r.status), (4, "ok"))
        finally:
            db.close()


class ResponseParsingTests(unittest.TestCase):
    def test_parse_normal_kline(self):  # Test 1
        candles, issues, times = klines("kline_normal.json")
        self.assertEqual(issues, [])
        self.assertEqual(len(candles), 5)
        first = min(candles, key=lambda c: c.open_time)
        self.assertEqual(first.open_time, T0)
        self.assertEqual((first.open, first.high, first.low, first.close),
                         (D("87000.1"), D("87120.5"), D("86950.0"), D("87080.2")))
        self.assertEqual(first.volume, D("152.341"))
        self.assertIsInstance(first.turnover, D)
        self.assertTrue(all(c.is_closed for c in candles))
        self.assertEqual(times, sorted(times, reverse=True))  # Bybit newest-first

    def test_empty_response(self):
        candles, issues, times = klines("kline_empty.json")
        self.assertEqual((candles, issues, times), ([], [], []))

    def test_open_candle_flagged(self):
        candles, _, _ = klines("kline_with_open.json", now=T0 + timedelta(minutes=65))
        open_ = [c for c in candles if not c.is_closed]
        self.assertEqual(len(open_), 1)
        self.assertEqual(open_[0].open_time, T0 + timedelta(hours=1))

    def test_instruments_linear(self):
        result, _ = parse_envelope(fixture_bytes("instruments_linear.json"), "x")
        items, issues, cursor = parse_instruments(result, Category.LINEAR)
        i = items[0]
        self.assertEqual((i.symbol.name, i.base_coin, i.quote_coin, i.status), ("BTCUSDT", "BTC", "USDT", "Trading"))
        self.assertEqual((i.tick_size, i.qty_step, i.min_qty, i.min_notional), (D("0.10"), D("0.001"), D("0.001"), D("5")))
        self.assertEqual((i.max_qty, i.max_market_qty, i.price_scale), (D("1190.000"), D("500.000"), 2))
        self.assertEqual((i.max_leverage, i.funding_interval_min), (D("100.00"), 480))
        self.assertEqual(i.launch_time, epoch_ms_to_utc(1585526400000))
        self.assertEqual((issues, cursor), ([], ""))

    def test_instruments_spot_no_invented_fields(self):
        result, _ = parse_envelope(fixture_bytes("instruments_spot.json"), "x")
        items, _, _ = parse_instruments(result, Category.SPOT)
        i = items[0]
        self.assertEqual(i.qty_step, D("0.000001"))        # basePrecision
        self.assertEqual(i.min_notional, D("1"))           # minOrderAmt
        for absent in ("max_leverage", "funding_interval_min", "contract_type", "launch_time", "max_market_qty"):
            self.assertIsNone(getattr(i, absent), absent)

    def test_instrument_with_bad_field_is_rejected_not_invented(self):
        result, _ = parse_envelope(fixture_bytes("instruments_linear_page2.json"), "x")
        items, issues, _ = parse_instruments(result, Category.LINEAR)
        self.assertEqual([i.symbol.name for i in items], ["NEWUSDT"])
        self.assertEqual(len(issues), 1)
        self.assertIn("BROKENUSDT", issues[0].detail)

    def test_tickers_and_funding_and_time(self):
        result, env = parse_envelope(fixture_bytes("tickers_linear.json"), "x")
        (t,), _ = parse_tickers(result, Category.LINEAR, epoch_ms_to_utc(env))
        self.assertEqual((t.last, t.bid, t.ask), (D("87060.00"), D("87059.90"), D("87060.00")))
        self.assertEqual((t.volume_24h, t.turnover_24h), (D("67655.412"), D("5871234567.8811")))
        result, _ = parse_envelope(fixture_bytes("funding_history.json"), "x")
        rates, issues, _ = parse_funding(result, BTC)
        self.assertEqual([r.rate for r in rates], [D("0.0001"), D("0.00008512"), D("-0.00002113")])
        result, env = parse_envelope(fixture_bytes("server_time.json"), "x")
        self.assertEqual(parse_server_time(result, env), epoch_ms_to_utc(1767229200123))

    def test_symbol_mismatch_is_malformed(self):
        with self.assertRaises(MalformedResponse):
            klines("kline_wrong_symbol.json")


class NormalizationTests(unittest.TestCase):
    def test_interval_mapping_is_bijective(self):
        self.assertEqual(set(TO_BYBIT_INTERVAL), set(Timeframe))
        self.assertEqual({v: k for k, v in FROM_BYBIT_INTERVAL.items()}, TO_BYBIT_INTERVAL)
        self.assertEqual(TO_BYBIT_INTERVAL[Timeframe.H4], "240")
        self.assertEqual(TO_BYBIT_INTERVAL[Timeframe.D1], "D")

    def test_timeframe_single_representation(self):
        self.assertIs(Timeframe.parse("15m"), Timeframe.M15)
        self.assertIs(Timeframe.parse(" 1H "), Timeframe.H1)
        for bad in ("15", "900", "60", "D", "1w", "", 15):
            with self.subTest(bad=bad), self.assertRaises(DomainError):
                Timeframe.parse(bad)
        self.assertEqual(Timeframe.H4.ms, 14_400_000)

    def test_units_volume_base_turnover_quote(self):
        candles, issues, _ = klines("kline_normal.json")
        for c in candles:  # vwap = turnover(quote) / volume(base) must be a price inside the bar
            self.assertTrue(c.low <= c.turnover / c.volume <= c.high)

    def test_swapped_units_detected(self):
        p = make_provider(ScriptedTransport(resp("kline_units_swapped.json")))
        batch = p.candles(BTC, M15, T0, T0 + M15.delta * 5)
        kinds = [i.kind for i in batch.issues]
        self.assertEqual(kinds.count("unit_suspect"), 1)

    def test_decimal_exact(self):
        candles, _, _ = klines("kline_normal.json")
        self.assertTrue(all(isinstance(c.close, D) for c in candles))
        self.assertIn(D("87190.9"), [c.close for c in candles])


class TimestampTests(unittest.TestCase):  # Test 7
    def test_representations_normalise_to_same_utc(self):
        msk = timezone(timedelta(hours=3))
        expected = T0
        for v in (T0_MS, str(T0_MS), datetime(2026, 1, 1, 3, 0, tzinfo=msk), T0):
            with self.subTest(v=v):
                got = normalize_timestamp(v)
                self.assertEqual(got, expected)
                self.assertEqual(got.utcoffset(), timedelta(0))
        self.assertEqual(epoch_seconds_to_utc(T0_MS // 1000), expected)
        self.assertEqual(utc_to_epoch_ms(expected), T0_MS)

    def test_ambiguous_or_wrong_inputs_rejected(self):
        for bad in (T0_MS // 1000, str(T0_MS // 1000), float(T0_MS), "17e11", "-1", True,
                    datetime(2026, 1, 1)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_timestamp(bad)
        with self.assertRaisesRegex(ValueError, "SECONDS"):
            epoch_ms_to_utc(T0_MS // 1000)

    def test_candle_boundaries(self):
        t = T0 + timedelta(minutes=37, seconds=5)
        self.assertEqual(M15.floor(t), T0 + timedelta(minutes=30))
        self.assertEqual(M15.ceil(t), T0 + timedelta(minutes=45))
        self.assertEqual(M15.ceil(T0), T0)
        self.assertEqual(Timeframe.H4.floor(T0 + timedelta(hours=5)), T0 + timedelta(hours=4))
        self.assertEqual(Timeframe.D1.floor(T0 + timedelta(hours=23)), T0)
        c = Candle(BTC, M15, T0, D(1), D(1), D(1), D(1), D(0))
        self.assertEqual(c.close_time, T0 + timedelta(minutes=15))
        with self.assertRaises(DomainError):
            Candle(BTC, M15, T0 + timedelta(minutes=1), D(1), D(1), D(1), D(1), D(0))

    def test_ordering_normalised_ascending(self):
        p = make_provider(ScriptedTransport(resp("kline_out_of_order.json")))
        batch = p.candles(BTC, M15, T0, T0 + M15.delta * 5)
        times = [c.open_time for c in batch.candles]
        self.assertEqual(times, sorted(times))
        self.assertEqual(len(times), 5)
        self.assertIn("out_of_order", [i.kind for i in batch.issues])


class CandleInvariantTests(unittest.TestCase):  # Test 2
    def test_bad_ohlc_rejected_with_issue(self):
        candles, issues, _ = klines("kline_bad_ohlc.json")
        self.assertEqual(len(candles), 4)
        self.assertEqual(len(issues), 1)
        self.assertIn("OHLC", issues[0].detail)

    def test_invalid_numbers_rejected(self):
        candles, issues, times = klines("kline_invalid_number.json")
        self.assertEqual(len(candles), 3)
        self.assertEqual(len(issues), 2)
        self.assertEqual(len(times), 5)   # timestamps still parsed -> pagination unaffected
        self.assertTrue(any("abc" in i.detail for i in issues))
        self.assertTrue(any("NaN" in i.detail for i in issues))

    def test_misaligned_rejected(self):
        candles, issues, _ = klines("kline_misaligned.json")
        self.assertEqual(len(candles), 4)
        self.assertIn("aligned", issues[0].detail)

    def test_domain_invariants(self):
        ok = dict(symbol=BTC, timeframe=M15, open_time=T0, open=D(10), high=D(12), low=D(9), close=D(11),
                  volume=D(1), turnover=D(10))
        Candle(**ok)
        for bad in ({"high": D(8)}, {"low": D(11)}, {"volume": D(-1)}, {"turnover": D(-1)},
                    {"open": D(0)}, {"close": D("NaN")}):
            with self.subTest(bad=bad), self.assertRaises(DomainError):
                Candle(**{**ok, **bad})
        Candle(**{**ok, "open": D(10), "high": D(10), "low": D(10), "close": D(10), "volume": D(0),
                  "turnover": D(0)})  # no-trade bar is legal


class FixtureTests(unittest.TestCase):
    REQUIRED = ["kline_normal.json", "kline_empty.json", "api_error_10001.json", "malformed.txt",
                "kline_duplicates.json", "kline_out_of_order.json", "kline_page1.json", "kline_page2.json",
                "kline_gap.json", "kline_invalid_number.json", "kline_missing_list.json"]

    def test_required_fixtures_present(self):
        for n in self.REQUIRED:
            self.assertTrue((FIX / n).exists(), n)

    def test_json_fixtures_use_bybit_envelope(self):
        for f in FIX.glob("*.json"):
            if f.name == "truncated.json":
                continue
            with self.subTest(f=f.name):
                doc = json.loads(f.read_text())
                self.assertEqual(set(doc) >= {"retCode", "retMsg", "result", "time"}, True)
                self.assertIsInstance(doc["time"], int)

    def test_malformed_and_truncated(self):
        for name in ("malformed.txt", "truncated.json"):
            with self.subTest(name=name), self.assertRaises(MalformedResponse):
                parse_envelope(fixture_bytes(name), "/v5/market/kline")

    def test_missing_list_and_bad_row_shape(self):
        for name in ("kline_missing_list.json", "kline_bad_row_shape.json"):
            with self.subTest(name=name), self.assertRaises(MalformedResponse):
                klines(name)

    def test_api_error_fixture(self):
        with self.assertRaises(BybitApiError) as cm:
            parse_envelope(fixture_bytes("api_error_10001.json"), "/v5/market/kline")
        self.assertEqual(cm.exception.ret_code, 10001)
        self.assertFalse(cm.exception.retryable)
