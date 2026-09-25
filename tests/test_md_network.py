"""REAL Bybit public API smoke test. Opt-in only: python test.py --network
Never part of the offline baseline. No keys, no private endpoints, no orders."""
import os
import unittest
from datetime import timedelta

NETWORK = os.environ.get("QRE_NETWORK_TESTS") == "1"


@unittest.skipUnless(NETWORK, "network tests disabled (use: python test.py --network)")
class BybitSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.config.settings import load_settings
        from app.data.bybit.client import BybitRestClient
        from app.data.bybit.provider import BybitMarketDataProvider
        from app.data.http import RequestExecutor, UrllibTransport
        s = load_settings()
        ex = RequestExecutor(UrllibTransport(), timeout_s=float(s.http_timeout_s))
        cls.p = BybitMarketDataProvider(BybitRestClient(s.market_data_rest_url, ex))

    def test_server_time(self):
        from datetime import datetime, timezone
        t = self.p.server_time()
        self.assertLess(abs((t - datetime.now(timezone.utc)).total_seconds()), 60, "clock skew > 60s")

    def test_instrument_info(self):
        from app.domain.enums import Category
        (btc,) = self.p.instruments(Category.LINEAR, ["BTCUSDT"])
        self.assertTrue(btc.is_trading)
        self.assertGreater(btc.tick_size, 0)

    def test_candles_parse(self):
        from app.domain.market import Symbol, Timeframe
        now = self.p.server_time()
        b = self.p.candles(Symbol("BTCUSDT"), Timeframe.H1, now - timedelta(hours=6), now + timedelta(hours=1))
        self.assertGreaterEqual(len(b.closed), 5)
        self.assertEqual([i for i in b.issues if i.kind != "out_of_range"], [])
