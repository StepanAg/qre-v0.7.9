"""Phase 6 with the real Regime/Structure services, inside the monitor, via CLI."""
import contextlib
import io
import os
import unittest
from datetime import timedelta
from unittest import mock

from app.core.errors import StorageError
from app.domain.enums import QualityStatus
from app.domain.market import Symbol, Timeframe
from app.monitor.health import SetupStepStatus, StructureStatus, UnitStatus
from tests.monitor_harness import Rig

ETHS, M15 = Symbol("ETHUSDT"), Timeframe.M15


def count(rig, table):
    return rig.market.writer.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class SetupWithRegimeAndStructureTests(unittest.TestCase):
    def test_contexts_link_to_live_snapshots(self):
        from app.research.regime.config import RegimeConfig
        from app.research.regime.service import RegimeService
        from app.research.service import FeatureService
        from app.research.setup.config import SetupConfig
        from app.research.setup.service import SetupService
        from app.research.structure.config import StructureConfig
        from app.research.structure.service import StructureService
        from tests.fakes import TempDB
        from tests.regime_data import AS_OF, BTC, CONFIG_FILE, ETH, H1, H4, series
        from tests.setup_data import SETUP_CONFIG
        from tests.structure_data import CONFIG_FILE as SCFG
        db = TempDB()
        try:
            for tf in (M15, H1, H4):
                db.store.upsert_candles(series("turn_down", tf, symbol=ETH), "t")
            db.store.upsert_candles(series("up", H1, symbol=BTC), "t")
            regime = RegimeService(FeatureService(db.store), RegimeConfig.from_file(CONFIG_FILE)).analyze(ETH, AS_OF, M15)
            structure = StructureService(db.store, StructureConfig.from_file(SCFG))
            svc = SetupService(db.store, structure, SetupConfig.from_file(SETUP_CONFIG))
            s = svc.analyze(ETH, M15, AS_OF, regime)
            self.assertEqual(s.data_quality, QualityStatus.VALID)
            live = structure.analyze(ETH, M15, AS_OF)
            self.assertEqual(s.structure_context["input_fingerprint"], live.input_fingerprint)   # same facts as live
            self.assertEqual(s.regime_context["input_fingerprint"], regime.input_fingerprint)
            self.assertEqual(s.regime_context["regime"], regime.regime.value)
            self.assertTrue(all(x.regime_fit.value in ("allowed", "not_allowed") for x in s.setups))
        finally:
            db.close()


class MonitorSetupTests(unittest.TestCase):
    def test_pipeline_regime_structure_setup(self):
        r = Rig()
        try:
            r.monitor.tick()
            for key in ("ETHUSDT/15m", "ETHUSDT/1h"):
                u = r.unit(key)
                self.assertEqual((u.status, u.structure_status, u.setup_status),
                                 (UnitStatus.OK, StructureStatus.OK, SetupStepStatus.OK))
            self.assertEqual(count(r, "setup_snapshots"), 2)
            h = r.monitor.health.to_dict()
            self.assertEqual(h["setup"]["snapshots_created"], 2)
            self.assertEqual(h["units"]["ETHUSDT/15m"]["setup"]["status"], "ok")
            self.assertIsNotNone(h["units"]["ETHUSDT/15m"]["setup"]["active_setups"])
        finally:
            r.close()

    def test_setup_failure_is_isolated_and_retried_alone(self):
        r = Rig()
        try:
            real = r.monitor.setups.analyze
            calls = {"n": 0}

            def flaky(*a, **k):
                calls["n"] += 1
                if calls["n"] <= 2:
                    raise RuntimeError("setup bug")
                return real(*a, **k)
            r.monitor.setups.analyze = flaky
            r.monitor.tick()
            u = r.unit("ETHUSDT/15m")
            self.assertEqual((u.status, u.structure_status, u.setup_status),
                             (UnitStatus.OK, StructureStatus.OK, SetupStepStatus.ERROR))
            before = (count(r, "regime_snapshots"), count(r, "structure_snapshots"),
                      len(r.runs.events(r.monitor.health.run_id)))
            r.advance(30)
            r.monitor.tick()                                        # same bar: only the setup step reruns
            self.assertEqual(u.setup_status, SetupStepStatus.OK)
            self.assertEqual((count(r, "regime_snapshots"), count(r, "structure_snapshots"),
                              len(r.runs.events(r.monitor.health.run_id))), before)
            self.assertEqual(count(r, "setup_snapshots"), 2)
        finally:
            r.close()

    def test_persistence_error_bounded(self):
        r = Rig()
        try:
            class Broken:
                last = None

                def save(self, s):
                    raise StorageError("disk full")
            r.monitor.setups.store = Broken()
            for _ in range(5):
                r.monitor.tick()
                r.advance(30)
            u = r.unit("ETHUSDT/15m")
            self.assertEqual((u.setup_status, u.status), (SetupStepStatus.RETRY_EXHAUSTED, UnitStatus.OK))
            self.assertEqual(count(r, "regime_snapshots"), 2)
        finally:
            r.close()

    def test_no_duplicates_across_cycles_and_restart(self):
        r = Rig()
        try:
            for _ in range(61):
                r.monitor.tick()
                r.advance(30)
            n = (count(r, "setup_snapshots"), count(r, "setups"), count(r, "setup_events"))
            self.assertEqual(n[0], 4)                                # 15m: 12:00, 12:15, 12:30; 1h: 12:00
            self.assertEqual(count(r, "setup_conflicts"), 0)
            r.market.writer.conn.close()
            r2 = Rig(db_dir=r.dir, exchange=r.ex, at=r.clock.now())
            r2.monitor.tick()
            self.assertEqual((count(r2, "setup_snapshots"), count(r2, "setups"), count(r2, "setup_events")), n)
            r2.close()
        finally:
            import shutil
            shutil.rmtree(r.dir, ignore_errors=True)

    def test_can_be_disabled_and_supervisor_passes_the_flag(self):
        from app.cli.main import build_parser
        from app.cli.monitor_cmds import build_monitor, child_args
        r = Rig()
        try:
            m, _ = build_monitor(r.settings, r.cfg, transport=r.ex, clock=r.clock, waiter=r.time, setups=False)
            m.backfill.provider.client.executor._sleep = lambda s: None
            m.tick()
            self.assertIsNone(m.setups)
            self.assertFalse(m.health.to_dict()["setup"]["enabled"])
        finally:
            r.close()
        a = build_parser().parse_args(["monitor", "supervise", "--hours", "24", "--no-setups", "--symbols", "BTCUSDT"])
        self.assertEqual(child_args(a), ["--symbols", "BTCUSDT", "--no-setups"])

    def test_monitor_health_cli_shows_setup_state(self):
        r = Rig()
        try:
            r.time.hooks[1] = r.monitor.request_stop
            r.monitor.run()
            from app.cli.main import main
            out = io.StringIO()
            with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, {"DB_PATH": str(r.dir / "mon.sqlite3")}):
                self.assertEqual(main(["monitor", "health"]), 0)
            self.assertIn('"setup"', out.getvalue())
        finally:
            r.close()


class SetupCliTests(unittest.TestCase):
    def setUp(self):
        from tests.fakes import TempDB
        from tests.setup_data import PATH_R, W
        from tests.structure_data import candles
        self.db = TempDB()
        self.db.store.upsert_candles(candles(PATH_R, warmup=W), "t")
        self.env = {"DB_PATH": str(self.db.path)}

    def tearDown(self):
        self.db.close()

    def run_cli(self, *argv):
        from app.cli.main import main
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, self.env):
            code = main(list(argv))
        return code, out.getvalue()

    def test_full_cli_cycle(self):
        from tests.setup_data import W
        from tests.structure_data import bar_close
        at = bar_close(11, warmup=W).isoformat()
        code, out = self.run_cli("setup", "analyze", "--symbol", "ETHUSDT", "--tf", "15m", "--at", at, "--save",
                                 "--no-regime")
        self.assertEqual(code, 0)
        self.assertIn("range_rejection", out)
        self.assertIn("NOT trading signals", out)
        self.assertIn("saved: snapshot new, setups new=1", out)
        code, out = self.run_cli("setup", "analyze", "--symbol", "ETHUSDT", "--tf", "15m", "--at", at, "--save",
                                 "--no-regime")
        self.assertIn("already stored, setups new=0", out)
        self.assertEqual(self.run_cli("setup", "last", "--symbol", "ETHUSDT", "--tf", "15m")[0], 0)
        code, out = self.run_cli("setup", "active", "--symbol", "ETHUSDT", "--tf", "15m")
        self.assertIn("invalidation: close > 110.0 (range top)", out)
        code, out = self.run_cli("setup", "list", "--symbol", "ETHUSDT", "--tf", "15m")
        sid = out.split("id ")[-1].strip()
        code, out = self.run_cli("setup", "events", "--setup-id", sid)
        self.assertIn("candidate", out)
        self.assertIn("confirmed", out)
        code, out = self.run_cli("setup", "status")
        self.assertIn('"setups": 1', out)

    def test_with_regime_context_on_incomplete_data(self):
        from tests.setup_data import W
        from tests.structure_data import bar_close
        code, out = self.run_cli("setup", "analyze", "--symbol", "ETHUSDT", "--tf", "15m",
                                 "--at", bar_close(11, warmup=W).isoformat())
        self.assertEqual(code, 0)
        self.assertIn("regime context: unknown", out)              # regime honest about missing 1h/4h/BTC data
        self.assertIn("regime_fit=unknown", out)

    def test_empty_database_is_clear(self):
        code, out = self.run_cli("setup", "last", "--symbol", "BTCUSDT", "--tf", "1h")
        self.assertEqual(code, 1)
        self.assertIn("no setup snapshot stored", out)
