import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from app.config.env import parse_env_file
from app.config.settings import BybitEnv, ExecutionMode, load_settings
from app.core.errors import ConfigError

ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def test_defaults_without_env(self):
        s = load_settings(environ={}, env_file=None)
        self.assertEqual(s.execution_mode, ExecutionMode.DISABLED)
        self.assertEqual(s.bybit_env, BybitEnv.TESTNET)
        self.assertIn("testnet", s.bybit_rest_url)
        self.assertEqual(s.risk.risk_per_trade_pct, Decimal("0.5"))
        self.assertFalse(s.bybit_api_key.is_set())
        self.assertEqual(s.market_data_rest_url, "https://api.bybit.com")  # mainnet data even on testnet

    def test_override_and_endpoints(self):
        s = load_settings(environ={"BYBIT_ENV": "demo", "SYMBOLS": "solusdt, btcusdt",
                                   "TIMEFRAMES": "5m,1h", "EXECUTION_MODE": "paper"}, env_file=None)
        self.assertEqual(s.symbols, ("SOLUSDT", "BTCUSDT"))
        self.assertEqual([t.value for t in s.timeframes], ["5m", "1h"])
        self.assertEqual(s.bybit_rest_url, "https://api-demo.bybit.com")
        self.assertEqual(s.execution_mode, ExecutionMode.PAPER)

    def test_invalid_values_rejected(self):
        for env in ({"BYBIT_ENV": "moon"}, {"TIMEFRAMES": "7"}, {"TIMEFRAMES": "15"}, {"TIMEFRAMES": "900"},
                    {"MARKET_DATA_REST_URL": "http://api.bybit.com"}, {"KLINE_PAGE_LIMIT": "5000"}, {"LIVE_TRADING_ENABLED": "maybe"},
                    {"RISK_PER_TRADE_PCT": "5"}, {"AI_PROVIDER": "skynet"}, {"SYMBOLS": " , "},
                    {"KILL_SWITCH_DRAWDOWN_PCT": "25"}):
            with self.subTest(env=env), self.assertRaises(ConfigError):
                load_settings(environ=env, env_file=None)

    def test_env_file_parsing(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text('# c\nexport A=1\nB="x y"\nC=z # comment\n\n', encoding="utf-8")
            self.assertEqual(parse_env_file(p), {"A": "1", "B": "x y", "C": "z"})

    def test_env_example_is_loadable_and_safe(self):
        vals = parse_env_file(ROOT / ".env.example")
        s = load_settings(environ=vals, env_file=None)
        self.assertEqual(s.execution_mode, ExecutionMode.DISABLED)
        self.assertFalse(s.live_trading_enabled or s.spot_live_enabled or s.demo_trading_enabled)
        self.assertFalse(s.bybit_api_key.is_set(), ".env.example must hold placeholders only")

    def test_redacted_hides_secrets(self):
        s = load_settings(environ={"BYBIT_API_KEY": "realkey123456", "BYBIT_API_SECRET": "sec987654"},
                          env_file=None)
        dumped = str(s.redacted()) + repr(s)
        self.assertNotIn("realkey123456", dumped)
        self.assertNotIn("sec987654", dumped)
        self.assertEqual(s.bybit_api_key.get_secret_value(), "realkey123456")
