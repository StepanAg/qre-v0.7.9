"""Phase 8 CLI: every command on a fixture DB, JSON/CSV determinism, error codes,
and a read-only guarantee checked by table checksums + file hash around every command."""
import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.domain.enums import SimExitReason as X
from tests.analytics_data import M15, RUN, T0, AnalyticsLab, trade

COMMANDS = [
    ["summary"], ["data-quality"], ["monitor"], ["ai"], ["rejected-real"],
    ["trades", "--run", RUN], ["performance", "--run", RUN], ["risk", "--run", RUN], ["costs", "--run", RUN],
    ["exits", "--run", RUN], ["drawdown", "--run", RUN, "--curve", "r"], ["portfolio", "--run", RUN],
    ["periods", "--run", RUN, "--by", "day"], ["untraded", "--run", RUN], ["setups", "--run", RUN],
    ["regimes", "--run", RUN], ["horizons", "--run", RUN], ["funnel", "--run", RUN], ["report", "--run", RUN],
    ["setups", "--live"], ["regimes", "--live"], ["funnel", "--live"], ["compare", "--runs", RUN, "bt_second"],
]


class AnalyticsCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.storage.setup import SQLiteSetupStore
        from tests.setup_data import PATH_A, evaluate
        from tests.test_analytics import _bars, outcome
        snap = evaluate(PATH_A, 18)
        b = next(s for s in snap.setups if s.setup_type.value == "breakout")
        obs = [{"setup": s.to_dict(), "first_seen": s.setup_time.isoformat(), "last_seen": s.setup_time.isoformat(),
                "first_regime_fit": "allowed" if s is b else "unknown"} for s in snap.setups]
        trades = [trade(1, 199.0, setup_id=b.setup_id), trade(2, -101.0, reason=X.STOP),
                  trade(3, 20.0, direction="bearish", setup_type="pullback")]
        eq = [(T0 + M15 * k, 10000.0 + v) for k, v in enumerate([0, 199, 150, 98, 118])]
        cls.lab = AnalyticsLab(trades, equity=eq, observations=obs,
                               outcomes=[outcome("su_9", 5, forward_return=0.01, mfe=0.02, mae=0.01)],
                               summary={"rule": "fixture_rule", "skipped_signals": {"type_not_in_rule": 0}})
        cls.lab.add_backtest("bt_second", [trade(1, 5.0, run_id="bt_second")])
        cls.lab.db.store.upsert_candles(_bars([(100, 101, 99.5, 100.8), (100.8, 103, 100.5, 102)]), "t")
        SQLiteSetupStore(cls.lab.db.writer).save(snap)
        cls.env = {"DB_PATH": str(cls.lab.path)}

    @classmethod
    def tearDownClass(cls):
        cls.lab.close()

    def cli(self, *argv):
        from app.cli.main import main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.dict(os.environ, self.env):
            code = main(["analytics", *argv])
        return code, out.getvalue(), err.getvalue()

    def fingerprint(self):
        c = self.lab.db.conn
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        h = hashlib.sha256()
        for t in tables:
            for row in c.execute(f'SELECT * FROM "{t}" ORDER BY 1'):
                h.update(repr(tuple(row)).encode())
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return h.hexdigest(), hashlib.sha256(Path(self.lab.path).read_bytes()).hexdigest()

    def test_every_command_runs_and_is_read_only(self):
        before = self.fingerprint()
        for argv in COMMANDS:
            for fmt in ("text", "json"):
                with self.subTest(argv=argv, fmt=fmt):
                    code, out, err = self.cli(*argv, "--format", fmt)
                    self.assertEqual(code, 0, err)
                    if fmt == "json":
                        env = json.loads(out)
                        self.assertEqual(set(env), {"source", "filters", "as_of", "versions", "warnings", "data"})
            self.assertEqual(self.fingerprint(), before, f"{argv} changed the database")

    def test_json_is_deterministic_and_as_of_comes_from_data(self):
        a = self.cli("report", "--run", RUN, "--format", "json")[1]
        b = self.cli("report", "--run", RUN, "--format", "json")[1]
        self.assertEqual(a, b)
        env = json.loads(a)
        # as_of = latest timestamp of the data used: trade 3 exits at bar 30 + 2 = 08:00 (later than the
        # last equity point 01:00) - never the wall clock
        self.assertEqual(env["as_of"], (T0 + M15 * 32).isoformat())
        self.assertEqual(env["data"]["ai"]["reason"], "source_not_available")
        self.assertIn("legacy_metric_semantics", env["data"]["stored_phase7_reports"])
        self.assertTrue(env["data"]["limitations"])

    def test_values_through_the_cli(self):
        p = json.loads(self.cli("performance", "--run", RUN, "--format", "json")[1])["data"]
        self.assertAlmostEqual(p["net_pnl"]["value"], 118.0)                # 199 - 101 + 20
        self.assertEqual(p["win_rate"]["value"], 2 / 3)
        rf = json.loads(self.cli("regimes", "--run", RUN, "--format", "json")[1])["data"]
        self.assertEqual({k: v["trades"]["value"] for k, v in rf["by_regime_fit"].items()},
                         {"allowed": 1, "unknown": 2})
        self.assertEqual(rf["regime_label"]["reason"], "source_not_available")
        t = json.loads(self.cli("trades", "--run", RUN, "--format", "json")[1])["data"]
        self.assertAlmostEqual(t["trades"][0]["excursions"]["mfe_pct"], 0.03)   # candles stored for trade 1 only
        self.assertEqual(t["trades"][1]["excursions"]["reason"], "gap_in_trade_window")
        f = json.loads(self.cli("performance", "--run", RUN, "--direction", "bullish", "--format", "json")[1])
        self.assertEqual(f["data"]["trades"], {"value": 2})
        self.assertEqual(f["data"]["cumulative_return"]["reason"], "equity_not_filterable")
        self.assertEqual(f["filters"], {"direction": "bullish"})
        live = json.loads(self.cli("funnel", "--live", "--format", "json")[1])
        self.assertEqual((live["source"], live["data"]["total"]["candidates"]), ("live", {"value": 2}))
        cmp_ = json.loads(self.cli("compare", "--runs", RUN, "bt_second", "--format", "json")[1])["data"]
        self.assertTrue(cmp_["comparable"])

    def test_csv(self):
        code, out, _ = self.cli("periods", "--run", RUN, "--by", "day", "--format", "csv")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "period,by,trades,net_pnl,return,median_r,expectancy,profit_factor,"
                                              "max_drawdown,fees,funding,slippage")
        code, out, _ = self.cli("data-quality", "--format", "csv")
        self.assertEqual(out.splitlines()[0], "severity,table,record,category,problem")
        code, _, err = self.cli("risk", "--run", RUN, "--format", "csv")
        self.assertEqual(code, 2)
        self.assertIn("use --format json", err)

    def test_errors_and_output_file(self):
        self.assertEqual(self.cli("performance", "--run", "bt_missing")[0], 2)
        self.assertEqual(self.cli("performance", "--run", "rp_fixture")[0], 2)        # not a backtest run
        self.assertEqual(self.cli("performance")[0], 2)                                  # --run missing
        self.assertEqual(self.cli("performance", "--run", RUN, "--regime", "ranging")[0], 2)   # D1
        self.assertEqual(self.cli("performance", "--run", RUN, "--from", "2026-03-01")[0], 2)  # naive time
        self.assertEqual(self.cli("setups", "--run", RUN, "--live")[0], 2)
        out = Path(tempfile.mkdtemp()) / "r.json"
        code, msg, _ = self.cli("report", "--run", RUN, "--format", "json", "--out", str(out))
        self.assertEqual((code, json.loads(out.read_text())["source"]), (0, f"research:{RUN}"))
        with mock.patch.dict(os.environ, {"DB_PATH": str(Path(tempfile.mkdtemp()) / "none.sqlite3")}):
            from app.cli.main import main
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["analytics", "summary"]), 1)
