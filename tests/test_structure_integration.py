"""Phase 5 inside the Phase 4 monitor, and the structure CLI (offline)."""
import contextlib
import io
import os
import unittest
from unittest import mock

from app.core.errors import StorageError
from app.domain.market import Symbol, Timeframe
from app.monitor.health import StructureStatus, UnitStatus
from tests.monitor_harness import Rig

ETH, M15 = Symbol("ETHUSDT"), Timeframe.M15


def count(rig, table):
    return rig.market.writer.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class MonitorStructureTests(unittest.TestCase):
    def test_pipeline_regime_then_structure(self):
        r = Rig()
        try:
            r.monitor.tick()
            for key in ("ETHUSDT/15m", "ETHUSDT/1h"):
                u = r.unit(key)
                self.assertEqual((u.status, u.structure_status), (UnitStatus.OK, StructureStatus.OK))
            self.assertEqual((count(r, "regime_snapshots"), count(r, "structure_snapshots")), (2, 2))
            h = r.monitor.health.to_dict()
            self.assertEqual(h["structure"]["snapshots_created"], 2)
            self.assertEqual(h["units"]["ETHUSDT/15m"]["structure"]["status"], "ok")
        finally:
            r.close()

    def test_structure_failure_does_not_touch_regime_and_is_retried(self):
        r = Rig()
        try:
            real = r.monitor.structure.analyze
            calls = {"n": 0}

            def flaky(*a, **k):
                calls["n"] += 1
                if calls["n"] <= 2:
                    raise RuntimeError("structure bug")
                return real(*a, **k)
            r.monitor.structure.analyze = flaky
            c = r.monitor.tick()
            u = r.unit("ETHUSDT/15m")
            self.assertEqual(u.status, UnitStatus.OK)                       # regime outcome unchanged
            self.assertEqual(u.structure_status, StructureStatus.ERROR)       # own status, not FEATURE_ERROR
            self.assertIn("RuntimeError", u.structure_detail)
            self.assertEqual(count(r, "regime_snapshots"), 2)                # regime snapshots kept
            self.assertGreaterEqual(c.errors, 1)
            regime_attempts = len(r.runs.events(r.monitor.health.run_id))
            r.advance(30)
            r.monitor.tick()                                                 # same bar: structure-only retry
            self.assertEqual(u.structure_status, StructureStatus.OK)
            self.assertEqual(len(r.runs.events(r.monitor.health.run_id)), regime_attempts)   # regime not re-run
            self.assertEqual(count(r, "regime_snapshots"), 2)
            self.assertEqual(count(r, "structure_snapshots"), 2)
        finally:
            r.close()

    def test_structure_persistence_error_and_retry_exhaustion(self):
        r = Rig()
        try:
            class Broken:
                last = None

                def save(self, s):
                    raise StorageError("disk full")
            r.monitor.structure.store = Broken()
            for _ in range(4):
                r.monitor.tick()
                r.advance(30)
            u = r.unit("ETHUSDT/15m")
            self.assertEqual(u.structure_status, StructureStatus.RETRY_EXHAUSTED)
            self.assertEqual(u.status, UnitStatus.OK)
            self.assertEqual(r.monitor.health.outcome_counts["structure_retry_exhausted"], 2)
            before = r.monitor.health.outcome_counts.get("structure_persistence_error", 0)
            r.advance(30)
            r.monitor.tick()                                                 # exhausted: no tight retry loop
            self.assertEqual(r.monitor.health.outcome_counts.get("structure_persistence_error", 0), before)
        finally:
            r.close()

    def test_no_duplicates_over_many_cycles_and_restart(self):
        r = Rig()
        try:
            for _ in range(61):                                             # 30 minutes
                r.monitor.tick()
                r.advance(30)
            n_snap, n_ev = count(r, "structure_snapshots"), count(r, "structure_events")
            self.assertEqual(n_snap, 4)                                      # 15m: 12:00, 12:15, 12:30; 1h: 12:00
            r.market.writer.conn.close()
            r2 = Rig(db_dir=r.dir, exchange=r.ex, at=r.clock.now())
            r2.monitor.tick()
            self.assertEqual(count(r2, "structure_snapshots"), n_snap)       # identical data -> duplicate, no row
            self.assertEqual(count(r2, "structure_events"), n_ev)
            r2.close()
        finally:
            import shutil
            shutil.rmtree(r.dir, ignore_errors=True)

    def test_insufficient_history_is_unavailable_not_error(self):
        from datetime import timedelta
        from tests.monitor_harness import START, mcfg
        r = Rig(mcfg(symbols=["SOLUSDT"]), listings={"SOLUSDT": START - timedelta(hours=10)})
        try:
            r.monitor.tick()
            u = r.unit("SOLUSDT/1h")
            self.assertEqual(u.structure_status, StructureStatus.UNAVAILABLE)
            self.assertEqual(u.structure_detail, "insufficient_history")
        finally:
            r.close()

    def test_structure_can_be_disabled(self):
        from app.cli.monitor_cmds import build_monitor
        r = Rig()
        try:
            m, _ = build_monitor(r.settings, r.cfg, transport=r.ex, clock=r.clock, waiter=r.time, structure=False)
            m.backfill.provider.client.executor._sleep = lambda s: None
            m.tick()
            self.assertIsNone(m.structure)
            self.assertIsNone(m.health.units["ETHUSDT/15m"].structure_status)
            self.assertFalse(m.health.to_dict()["structure"]["enabled"])
        finally:
            r.close()

    def test_monitor_cli_still_works(self):
        r = Rig()
        try:
            r.time.hooks[1] = r.monitor.request_stop
            r.monitor.run()
            from app.cli.main import main
            out = io.StringIO()
            with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, {"DB_PATH": str(r.dir / "mon.sqlite3")}):
                self.assertEqual(main(["monitor", "health"]), 0)
                self.assertEqual(main(["monitor", "last", "--symbol", "ETHUSDT", "--tf", "15m"]), 0)
            text = out.getvalue()
            self.assertIn("ETHUSDT 15m @ 2026-03-02T12:00:00+00:00", text)
            self.assertIn("structure @ 2026-03-02T12:00:00+00:00", text)
        finally:
            r.close()


class StructureCliTests(unittest.TestCase):
    def setUp(self):
        from tests.fakes import TempDB
        from tests.structure_data import PATH_B, candles
        self.db = TempDB()
        self.db.store.upsert_candles(candles(PATH_B), "t")
        self.env = {"DB_PATH": str(self.db.path)}

    def tearDown(self):
        self.db.close()

    def run_cli(self, *argv):
        from app.cli.main import main
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, self.env):
            code = main(list(argv))
        return code, out.getvalue()

    def test_analyze_save_last_events_liquidity_status(self):
        from tests.structure_data import bar_close
        at = bar_close(10).isoformat()
        code, out = self.run_cli("structure", "analyze", "--symbol", "ETHUSDT", "--tf", "15m", "--at", at, "--save")
        self.assertEqual(code, 0)
        self.assertIn("direction unknown", out)
        self.assertIn("saved: snapshot new", out)
        code, out = self.run_cli("structure", "analyze", "--symbol", "ETHUSDT", "--tf", "15m", "--at", at, "--save")
        self.assertIn("saved: snapshot already stored, events new=0", out)
        self.assertEqual(self.run_cli("structure", "last", "--symbol", "ETHUSDT", "--tf", "15m")[0], 0)
        code, out = self.run_cli("structure", "events", "--symbol", "ETHUSDT", "--tf", "15m")
        self.assertIn("sweep", out)
        code, out = self.run_cli("structure", "liquidity", "--symbol", "ETHUSDT", "--tf", "15m")
        self.assertIn("NOT order-book data", out)
        self.assertIn("equal_lows", out)
        code, out = self.run_cli("structure", "status")
        self.assertIn('"snapshots": 1', out)

    def test_last_without_data_is_clear(self):
        code, out = self.run_cli("structure", "last", "--symbol", "BTCUSDT", "--tf", "1h")
        self.assertEqual(code, 1)
        self.assertIn("no structure snapshot stored", out)

    def test_json_output(self):
        import json
        from tests.structure_data import bar_close
        code, out = self.run_cli("structure", "analyze", "--symbol", "ETHUSDT", "--tf", "15m",
                                 "--at", bar_close(10).isoformat(), "--json")
        self.assertEqual(json.loads(out)["equal_lows"][0]["price"], 100.0)
