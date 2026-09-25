"""Watchdog + resilience for long local runs. Real child processes (scripted),
no network; the monitor part uses the offline harness with fake time."""
import contextlib
import io
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app.cli.main import EXIT_CONFIG_ERROR, EXIT_TRANSIENT_ERROR
from app.cli.supervisor import Supervisor, SupervisorPolicy, db_heartbeat_reader

FAST = SupervisorPolicy(backoff_base_s=5, backoff_max_s=40, stable_after_s=3600, max_restarts_per_hour=20,
                        hang_after_s=0.3, startup_grace_s=0.3, check_interval_s=0.05, graceful_timeout_s=1)


def scripted(*steps):
    """Each launch runs the next step: (sleep_seconds, exit_code)."""
    plan = list(steps)
    launched = []

    def launch(cmd):
        launched.append(cmd)
        t, code = plan.pop(0)
        return subprocess.Popen([sys.executable, "-c", f"import sys,time; time.sleep({t}); sys.exit({code})"])
    return launch, launched


def fresh(now=lambda: datetime.now(timezone.utc)):
    return lambda since: now()          # heartbeat always current -> never "hung"


class SupervisorTests(unittest.TestCase):
    def sup(self, launch, heartbeat=None, policy=FAST, deadline=None):
        self.sleeps = []
        return Supervisor(["--symbols", "BTCUSDT"], deadline=deadline, heartbeat=heartbeat or fresh(),
                          policy=policy, sleep=self.sleeps.append, launcher=launch)

    def test_crash_then_restart_then_clean_exit(self):
        launch, launched = scripted((0, 1), (0, 0))
        rep = self.sup(launch).run()
        self.assertEqual((rep.outcome, rep.launches, rep.restarts), ("stopped", 2, 1))
        self.assertEqual(self.sleeps, [5])
        self.assertIn("exit code 1", rep.reasons[0])

    def test_configuration_error_is_not_restarted(self):
        launch, _ = scripted((0, EXIT_CONFIG_ERROR))
        rep = self.sup(launch).run()
        self.assertEqual((rep.outcome, rep.launches, rep.restarts), ("config_error", 1, 0))

    def test_transient_error_is_restarted(self):
        launch, _ = scripted((0, EXIT_TRANSIENT_ERROR), (0, 0))
        self.assertEqual(self.sup(launch).run().restarts, 1)

    def test_backoff_grows_and_is_capped(self):
        launch, _ = scripted(*[(0, 1)] * 5, (0, 0))
        self.sup(launch).run()
        self.assertEqual(self.sleeps, [5, 10, 20, 40, 40])

    def test_crash_loop_guard(self):
        policy = SupervisorPolicy(**{**FAST.__dict__, "max_restarts_per_hour": 3})
        launch, launched = scripted(*[(0, 1)] * 10)
        rep = self.sup(launch, policy=policy).run()
        self.assertEqual((rep.outcome, rep.launches, rep.restarts), ("gave_up", 4, 3))

    def test_hung_monitor_is_killed_and_restarted(self):
        launch, _ = scripted((30, 0), (0, 0))
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        t0 = datetime.now(timezone.utc)
        rep = self.sup(launch, heartbeat=lambda since: old).run()
        self.assertEqual((rep.launches, rep.restarts, rep.outcome), (2, 1, "stopped"))
        self.assertIn("hung", rep.reasons[0])
        self.assertLess((datetime.now(timezone.utc) - t0).total_seconds(), 15)   # killed, not waited 30 s

    def test_no_heartbeat_after_startup_grace_is_hang(self):
        launch, _ = scripted((30, 0), (0, 0))
        rep = self.sup(launch, heartbeat=lambda since: None).run()
        self.assertIn("hung", rep.reasons[0])

    def test_deadline_and_remaining_minutes(self):
        launch, launched = scripted((0, 0))
        dl = datetime.now(timezone.utc) + timedelta(minutes=90, seconds=20)
        self.sup(launch, deadline=dl).run()
        cmd = launched[0]
        self.assertEqual(cmd[1:6], ["-m", "app", "monitor", "run", "--symbols"])
        self.assertEqual(cmd[-2:], ["--minutes", "91"])
        launch, launched = scripted((0, 0))
        rep = self.sup(launch, deadline=datetime.now(timezone.utc) - timedelta(seconds=1)).run()
        self.assertEqual((rep.launches, rep.outcome), (0, "completed"))

    def test_policy_backoff_function(self):
        self.assertEqual([SupervisorPolicy().backoff(n) for n in (1, 2, 3, 7, 8, 20)], [5, 10, 20, 300, 300, 300])


class HeartbeatAndRunStateTests(unittest.TestCase):
    def test_db_heartbeat_reader(self):
        from app.storage.database import bootstrap
        from app.storage.monitor import SQLiteMonitorRunStore
        from app.storage.writer import SerializedWriter
        d = Path(tempfile.mkdtemp())
        db = d / "x.sqlite3"
        self.assertIsNone(db_heartbeat_reader(d / "missing.sqlite3")(datetime.now(timezone.utc)))
        conn = bootstrap(db, shared=True)
        store = SQLiteMonitorRunStore(SerializedWriter(conn))
        t0 = datetime(2026, 3, 2, 12, tzinfo=timezone.utc)
        store.start_run("r1", t0, {}, {})
        read = db_heartbeat_reader(db)
        self.assertEqual(read(t0 - timedelta(seconds=1)), t0)                  # no heartbeat yet -> start time
        store.heartbeat("r1", t0 + timedelta(minutes=3), "running", {})
        self.assertEqual(read(t0 - timedelta(seconds=1)), t0 + timedelta(minutes=3))
        self.assertIsNone(read(t0 + timedelta(hours=1)))                         # a run started earlier is not ours
        self.assertEqual(store.mark_abandoned(t0 + timedelta(hours=1)), ["r1"])
        self.assertEqual(store.run("r1")["status"], "abandoned")
        self.assertEqual(store.mark_abandoned(t0 + timedelta(hours=2)), [])
        conn.close()

    def test_exit_codes_distinguish_permanent_and_transient(self):
        from app.cli.main import main
        from app.core.errors import StorageError
        bad = Path(tempfile.mkdtemp()) / "bad.json"
        bad.write_text('{"schema": 1}')
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(main(["monitor", "run", "--config", str(bad)]), EXIT_CONFIG_ERROR)
            with mock.patch("app.cli.monitor_cmds._stores", side_effect=StorageError("database is locked")):
                self.assertEqual(main(["monitor", "health"]), EXIT_TRANSIENT_ERROR)

    def test_log_rotation(self):
        from logging.handlers import RotatingFileHandler
        from app.core.logging import add_file_handler, get_logger
        d = Path(tempfile.mkdtemp())
        h = add_file_handler(d / "m.log", max_bytes=2000, backups=2)
        try:
            self.assertIsInstance(h, RotatingFileHandler)
            lg = get_logger("rot")
            lg.setLevel(logging.INFO)
            logging.getLogger("qre").setLevel(logging.INFO)
            for i in range(200):
                lg.info("line %d %s", i, "x" * 50)
            files = sorted(p.name for p in d.iterdir())
            self.assertEqual(files, ["m.log", "m.log.1", "m.log.2"])     # bounded: never more than 3 files
            self.assertTrue(all((d / f).stat().st_size <= 2200 for f in files))
        finally:
            logging.getLogger("qre").removeHandler(h)
            h.close()

    def test_keep_awake_is_safe_everywhere(self):
        from app.cli.keepawake import keep_awake
        with keep_awake(False) as st:
            self.assertEqual(st, "disabled")
        with keep_awake(True) as st:
            self.assertTrue(st)

    def test_launchers_exist(self):
        root = Path(__file__).resolve().parents[1]
        self.assertIn("python -m app monitor supervise %*", (root / "run_monitor.bat").read_text())
        self.assertIn("python3 -m app monitor supervise", (root / "run_monitor.sh").read_text())


class MonitorResilienceTests(unittest.TestCase):
    """Internet outages and computer sleep are handled INSIDE the monitor."""

    def _gaps(self, r, tf, hours):
        from app.domain.market import Symbol
        ts = r.market.candle_times(Symbol("ETHUSDT"), tf, r.clock.now() - timedelta(hours=hours), r.clock.now())
        return len(ts), sum(1 for a, b in zip(ts, ts[1:]) if b - a != tf.delta)

    def test_two_hour_outage_recovers_without_gaps(self):
        from app.data.http import HttpResponse
        from app.domain.market import Timeframe
        from app.monitor.health import ApiStatus, UnitStatus
        from tests.monitor_harness import Rig
        r = Rig()
        try:
            r.monitor.tick()
            r.ex.fail(10 ** 6, HttpResponse(502, b"down", {}, "x"))
            for _ in range(24):                          # 2 h offline, polled every 5 min
                r.advance(300)
                r.monitor.tick()
            h = r.monitor.health
            self.assertEqual(h.api_status, ApiStatus.DOWN)
            self.assertLess(h.api_requests, 200)         # bounded retrying, no request storm
            r.ex.fail_queue.clear()
            r.advance(30)
            r.monitor.tick()                              # units still in backoff: waits, does not hammer
            self.assertEqual(h.api_status, ApiStatus.DOWN)
            r.advance(r.cfg.unit_backoff_max_s)           # recovery within one max backoff
            r.monitor.tick()
            self.assertEqual(h.api_status, ApiStatus.OK)
            self.assertEqual({u.status for u in h.units.values()}, {UnitStatus.OK})
            from app.domain.market import Symbol
            last = r.market.last_candles(Symbol("ETHUSDT"), Timeframe.M15, r.clock.now(), 1)[0]
            self.assertEqual(last.close_time, Timeframe.M15.floor(r.clock.now()))   # caught up to the last closed bar
            n, gaps = self._gaps(r, Timeframe.M15, 3)                                # whole outage span
            tf, now = Timeframe.M15, r.clock.now()
            slots = (tf.floor(now) - tf.ceil(now - timedelta(hours=3))) // tf.delta   # every 15m slot in the window
            self.assertEqual((n, gaps), (slots, 0))        # every missed 15m bar was back-filled
        finally:
            r.close()

    def test_computer_sleep_time_jump_catches_up(self):
        from app.domain.market import Timeframe
        from app.monitor.health import UnitStatus
        from tests.monitor_harness import Rig
        r = Rig()
        try:
            r.monitor.tick()
            r.advance(3 * 3600)                          # laptop slept 3 hours: no ticks at all
            r.monitor.tick()
            h = r.monitor.health
            self.assertEqual({u.status for u in h.units.values()}, {UnitStatus.OK})
            self.assertEqual(h.units["ETHUSDT/15m"].last_ok_as_of.strftime("%H:%M"), "15:00")
            self.assertEqual(self._gaps(r, Timeframe.M15, 4)[1], 0)
            self.assertEqual(self._gaps(r, Timeframe.H1, 4)[1], 0)
        finally:
            r.close()
