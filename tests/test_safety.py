import re
import unittest
from decimal import Decimal as D
from pathlib import Path

from app.config.settings import BUILD_EXECUTION_CEILING, ExecutionMode, load_settings
from app.core.errors import SafetyViolation
from app.domain.enums import Category, OrderType, Side
from app.domain.execution import Order
from app.execution.safety import DisabledGateway, OrderPermission
from tests.factories import BTC, ts

ROOT = Path(__file__).resolve().parents[1]


class SafetyTests(unittest.TestCase):
    def test_defaults_are_safe(self):
        s = load_settings(environ={}, env_file=None)
        self.assertFalse(s.live_trading_enabled)
        self.assertFalse(s.spot_live_enabled)
        self.assertFalse(s.demo_trading_enabled)
        self.assertEqual(s.execution_mode, ExecutionMode.DISABLED)

    def test_build_ceiling_is_paper(self):
        self.assertEqual(BUILD_EXECUTION_CEILING, ExecutionMode.PAPER)

    def test_live_flags_refused_at_startup(self):
        for env in ({"LIVE_TRADING_ENABLED": "true"}, {"SPOT_LIVE_ENABLED": "1"},
                    {"DEMO_TRADING_ENABLED": "yes"}, {"EXECUTION_MODE": "live"},
                    {"EXECUTION_MODE": "demo"}):
            with self.subTest(env=env), self.assertRaises(SafetyViolation):
                load_settings(environ=env, env_file=None)

    def test_no_exchange_order_possible(self):
        for mode in ("disabled", "paper"):
            s = load_settings(environ={"EXECUTION_MODE": mode}, env_file=None)
            for cat in Category:
                self.assertFalse(OrderPermission.check_exchange_submission(s, cat).allowed)
            o = Order("ord_1", "q1", BTC, Side.BUY, OrderType.MARKET, D("1"), ts(), ts())
            with self.assertRaises(SafetyViolation):
                DisabledGateway(s).submit(o)

    def test_no_order_endpoint_in_code(self):
        pattern = re.compile(r"/v5/order/(create|amend|cancel)|place_order|placeOrder")
        hits = [str(f.relative_to(ROOT)) for f in (ROOT / "app").rglob("*.py")
                if pattern.search(f.read_text(encoding="utf-8"))]
        self.assertEqual(hits, [])

    def test_no_hardcoded_secrets(self):
        pat = re.compile(r"(?i)(api[_-]?key|api[_-]?secret|secret)\s*=\s*[\"'][A-Za-z0-9]{12,}[\"']")
        hits = []
        for f in list((ROOT / "app").rglob("*.py")) + [ROOT / ".env.example"]:
            if pat.search(f.read_text(encoding="utf-8")):
                hits.append(str(f.relative_to(ROOT)))
        self.assertEqual(hits, [])

    def test_env_is_gitignored(self):
        gi = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn(".env", [l.strip() for l in gi])
