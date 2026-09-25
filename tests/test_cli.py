import contextlib
import io
import json
import os
import unittest
from unittest import mock

from app.cli.main import main


def run(*args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), mock.patch.dict(os.environ, {"EXECUTION_MODE": "disabled"}):
        code = main(list(args))
    return code, buf.getvalue()


class CliTests(unittest.TestCase):
    def test_version(self):
        code, out = run("version")
        self.assertEqual(code, 0)
        self.assertIn("0.7.0", out)
        self.assertNotIn("v7.0", out)

    def test_config_redacted(self):
        code, out = run("config")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertIn(data["bybit_api_key"], ("<empty>", "***set***"))

    def test_status_json(self):
        code, out = run("status", "--json")
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertFalse(data["exchange_orders_allowed"]["linear"])
        self.assertFalse(data["exchange_orders_allowed"]["spot"])

    def test_unsafe_env_gives_clean_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch.dict(os.environ, {"LIVE_TRADING_ENABLED": "true"}):
            code = main(["status"])
        self.assertEqual(code, 2)
        self.assertIn("SafetyViolation", err.getvalue())


class DataCliTests(unittest.TestCase):
    def test_data_commands_registered(self):
        from app.cli.main import build_parser
        p = build_parser()
        for argv in (["data", "status"], ["data", "gaps", "--symbol", "BTCUSDT", "--tf", "15m"],
                     ["data", "backfill", "--tf", "15m", "--bars", "100", "--for-features", "ema_200"]):
            self.assertEqual(p.parse_args(argv).command, "data")

    def test_for_features_range_uses_engine_requirements(self):
        import argparse
        from datetime import datetime, timezone
        from app.cli.data_cmds import _range
        from app.domain.market import Timeframe
        a = argparse.Namespace(end="2026-03-01T00:00+00:00", for_features=["ema_200"], bars=100,
                               start=None, days=30)
        with contextlib.redirect_stdout(io.StringIO()):
            start, end = _range(a, Timeframe.H1)
        self.assertEqual((end - start) // Timeframe.H1.delta, 100 + 801 - 1)

    def test_offline_backfill_via_wiring(self):
        """Composition root with a synthetic transport: same code path as production."""
        from datetime import timedelta
        from app.cli.data_cmds import build
        from app.config.settings import load_settings
        from app.domain.market import Symbol, Timeframe
        from tests.fakes import SyntheticBybit, T0
        import tempfile, shutil
        d = tempfile.mkdtemp()
        try:
            s = load_settings(environ={"DB_PATH": f"{d}/x.sqlite3"}, env_file=None)
            now = T0 + timedelta(days=3)
            svc, store, metrics = build(s, transport=SyntheticBybit(now, T0))
            from app.core.clock import FixedClock
            svc.clock = FixedClock(now)
            svc.provider.clock = FixedClock(now)
            svc.provider.client.executor.pacer.min_interval_s = 0
            r = svc.backfill(Symbol("BTCUSDT"), Timeframe.H1, T0, now)
            self.assertEqual((r.status, r.inserted), ("ok", 72))
            self.assertEqual(metrics.api_requests, 1)
        finally:
            shutil.rmtree(d, ignore_errors=True)
