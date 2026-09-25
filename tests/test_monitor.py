"""Market monitor runtime - fully offline (synthetic Bybit, fake time)."""
import ast
import contextlib
import io
import json
import os
import sqlite3
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from app.core.errors import StorageError
from app.data.errors import ConnectionFailed
from app.data.http import HttpResponse
from app.domain.errors import DomainError
from app.domain.market import Symbol, Timeframe
from app.monitor.config import MonitorConfig
from app.monitor.health import ApiStatus, RunStatus, UnitStatus
from tests.monitor_harness import ROOT, START, Rig, mcfg

M15, H1 = Timeframe.M15, Timeframe.H1
BAD_GATEWAY = HttpResponse(502, b"bad gateway", {}, "fake")


def statuses(rig):
    return {k: u.status for k, u in rig.monitor.health.units.items()}


def rows(rig, table):
    return rig.market.writer.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class MonitorCycleTests(unittest.TestCase):
    def setUp(self):
        self.r = Rig()

    def tearDown(self):
        self.r.close()

    def test_successful_cycle(self):
        c = self.r.monitor.tick()
        self.assertEqual(statuses(self.r), {"ETHUSDT/15m": UnitStatus.OK, "ETHUSDT/1h": UnitStatus.OK})
        h = self.r.monitor.health
        self.assertEqual((h.snapshots_created, h.analyses_run, h.api_status), (2, 2, ApiStatus.OK))
        self.assertIsNotNone(h.last_api_success_at)
        self.assertEqual(h.last_snapshot_saved_at, START)
        self.assertEqual(len(c.processed), 2)
        ev = self.r.runs.events(h.run_id)
        self.assertEqual([(e["symbol"], e["timeframe"], e["status"], e["persisted"]) for e in ev],
                         [("ETHUSDT", "15m", "ok", "saved"), ("ETHUSDT", "1h", "ok", "saved")])
        self.assertTrue(all(e["regime"] and e["cycle_id"] == c.cycle_id for e in ev))

    def test_no_new_closed_bar_means_no_requests_and_no_analysis(self):
        self.r.monitor.tick()
        calls, analyses = len(self.r.ex.kline_calls), self.r.monitor.health.analyses_run
        for _ in range(20):                               # 10 minutes of polling, same 15m bar
            self.r.advance(30)
            c = self.r.monitor.tick()
            self.assertEqual(c.processed, {})
        self.assertEqual(len(self.r.ex.kline_calls), calls)
        self.assertEqual(self.r.monitor.health.analyses_run, analyses)
        self.assertEqual(len(self.r.runs.events(self.r.monitor.health.run_id)), 2)   # not logged per tick

    def test_forming_candle_never_used_or_stored(self):
        self.r.advance(7 * 60)                            # 12:07:30, bar 12:00-12:15 is forming
        self.r.monitor.tick()
        last = self.r.market.last_candles(Symbol("ETHUSDT"), M15, START + timedelta(hours=1), 1)[0]
        self.assertEqual(last.close_time.isoformat(), "2026-03-02T12:00:00+00:00")
        snap = self.r.regime_store.history(Symbol("ETHUSDT"), M15, 1)[0]
        self.assertEqual(snap.as_of.isoformat(), "2026-03-02T12:00:00+00:00")

    def test_grace_period_before_expecting_a_bar(self):
        r = Rig(at=START - timedelta(seconds=25))          # 12:00:05 < close + 10 s grace
        try:
            r.monitor.tick()
            self.assertEqual(r.regime_store.history(Symbol("ETHUSDT"), M15, 1)[0].as_of.isoformat(),
                             "2026-03-02T11:45:00+00:00")
        finally:
            r.close()

    def test_new_bar_processed_incrementally(self):
        self.r.monitor.tick()
        n = len(self.r.ex.kline_calls)
        self.r.advance(15 * 60)
        c = self.r.monitor.tick()
        self.assertEqual(c.processed, {"ETHUSDT/15m": "ok"})     # 1h bar has not closed yet
        self.assertLessEqual(len(self.r.ex.kline_calls) - n, 3)  # only the new bars, not a new warmup


class MonitorDataConditionTests(unittest.TestCase):
    def test_warmup_insufficient_does_not_block_others(self):
        r = Rig(mcfg(symbols=["ETHUSDT", "SOLUSDT"]), listings={"SOLUSDT": START - timedelta(days=3)})
        try:
            r.monitor.tick()
            st = statuses(r)
            self.assertEqual(st["ETHUSDT/15m"], UnitStatus.OK)
            self.assertEqual(st["SOLUSDT/1h"], UnitStatus.WARMUP_INSUFFICIENT)
            u = r.unit("SOLUSDT/1h")
            self.assertEqual(u.bars_required, r.monitor.required_bars)
            self.assertEqual(u.bars_available, 71)       # listed 12:00:30 -> first full hour bar 13:00
            self.assertIn("71/", u.detail)
            sol_1h = r.ex.kline_calls.count(("SOLUSDT", "60"))
            r.advance(3600)
            r.monitor.tick()
            self.assertEqual(r.unit("SOLUSDT/1h").status, UnitStatus.WARMUP_INSUFFICIENT)
            self.assertEqual(r.unit("SOLUSDT/1h").bars_available, 72)
            self.assertLessEqual(r.ex.kline_calls.count(("SOLUSDT", "60")) - sol_1h, 1)   # no repeated warmup
        finally:
            r.close()

    def test_required_bars_come_from_feature_requirements(self):
        from app.research.regime.config import ALL_FEATURES
        from app.research.service import FeatureService
        r = Rig()
        try:
            self.assertEqual(r.monitor.required_bars, FeatureService(r.market).bars_per_value(list(ALL_FEATURES)))
            src = (ROOT / "app" / "monitor" / "runtime.py").read_text()
            self.assertNotIn("800", src)
            self.assertNotIn("801", src)
        finally:
            r.close()

    def test_one_unavailable_timeframe_does_not_stop_others(self):
        r = Rig(mcfg(timeframes=["15m", "1h", "4h"]), broken={("ETHUSDT", "240")})
        try:
            r.monitor.tick()
            st = statuses(r)
            self.assertEqual(st["ETHUSDT/4h"], UnitStatus.API_ERROR)
            self.assertEqual(st["ETHUSDT/15m"], UnitStatus.OK)
            self.assertEqual(st["ETHUSDT/1h"], UnitStatus.OK)
            snap = r.regime_store.history(Symbol("ETHUSDT"), M15, 1)[0]
            self.assertEqual(snap.mtf_alignment.value, "partial")          # 4h gap is visible, not hidden
            self.assertIn("ETHUSDT/4h", r.monitor.health.to_dict()["unavailable_units"])
        finally:
            r.close()

    def test_stale_bar_waiting_then_stale(self):
        missing = {("ETHUSDT", "15"): {1772452800000 - 900_000}}          # bar 11:45-12:00 never delivered
        r = Rig(missing=missing)
        try:
            r.monitor.tick()
            self.assertEqual(r.unit("ETHUSDT/15m").status, UnitStatus.WAITING_FOR_BAR)
            self.assertEqual(r.unit("ETHUSDT/1h").status, UnitStatus.OK)
            r.advance(60)
            r.monitor.tick()
            self.assertEqual(r.unit("ETHUSDT/15m").status, UnitStatus.WAITING_FOR_BAR)
            r.advance(300)
            r.monitor.tick()
            self.assertEqual(r.unit("ETHUSDT/15m").status, UnitStatus.STALE)
            self.assertIn("ETHUSDT/15m", r.monitor.health.to_dict()["stale_units"])
            r.advance(30)
            c = r.monitor.tick()
            self.assertNotIn("ETHUSDT/15m", c.processed)                  # STALE is terminal for that bar
        finally:
            r.close()

    def test_gap_is_data_quality_failure_not_retried(self):
        gap = 1772452800000 - 900_000 * 10
        r = Rig(missing={("ETHUSDT", "15"): {gap}})
        try:
            r.monitor.tick()
            u = r.unit("ETHUSDT/15m")
            self.assertEqual(u.status, UnitStatus.DATA_QUALITY_FAILURE)
            self.assertIn("GAP", u.detail)
            self.assertIsNone(u.next_attempt_at)
            snap = r.regime_store.history(Symbol("ETHUSDT"), M15, 1)[0]
            self.assertEqual((snap.regime.value, snap.data_quality.value), ("unknown", "gap"))
            r.advance(30)
            self.assertNotIn("ETHUSDT/15m", r.monitor.tick().processed)
        finally:
            r.close()


class MonitorRetryTests(unittest.TestCase):
    def test_temporary_api_error_then_recovery(self):
        r = Rig()
        try:
            r.ex.fail(30, BAD_GATEWAY)                    # outage: everything fails for a while
            r.monitor.tick()
            h = r.monitor.health
            self.assertEqual(set(statuses(r).values()), {UnitStatus.API_TEMPORARY_ERROR})
            self.assertEqual(h.api_status, ApiStatus.DOWN)
            self.assertEqual(h.snapshots_created, 0)
            self.assertIsNotNone(r.unit("ETHUSDT/15m").next_attempt_at)
            r.ex.fail_queue.clear()                       # network back
            r.advance(1)
            self.assertEqual(r.monitor.tick().processed, {})       # still in backoff: no tight loop
            r.advance(30)
            r.monitor.tick()
            self.assertEqual(set(statuses(r).values()), {UnitStatus.OK})
            self.assertEqual(h.api_status, ApiStatus.OK)
            self.assertEqual(rows(r, "regime_snapshots"), 2)
            self.assertEqual(r.unit("ETHUSDT/15m").consecutive_failures, 0)
        finally:
            r.close()

    def test_connection_errors_are_retryable(self):
        r = Rig()
        try:
            r.ex.fail(6, ConnectionFailed("no route to host"))
            r.monitor.tick()
            self.assertEqual(r.unit("ETHUSDT/15m").status, UnitStatus.API_TEMPORARY_ERROR)
        finally:
            r.close()

    def test_retry_exhausted_then_next_bar_recovers(self):
        r = Rig()
        try:
            r.ex.fail(10_000, BAD_GATEWAY)
            for _ in range(12):                           # 6 minutes: backoff 5, 10, 20 ... capped at 60
                r.monitor.tick()
                r.advance(30)
            u = r.unit("ETHUSDT/15m")
            self.assertEqual(u.status, UnitStatus.RETRY_EXHAUSTED)
            self.assertEqual(r.monitor.health.outcome_counts["retry_exhausted"], 2)
            attempts = [e for e in r.runs.events(r.monitor.health.run_id) if e["timeframe"] == "15m"]
            self.assertEqual(len(attempts), 3)            # exactly unit_max_attempts for that bar
            r.ex.fail_queue.clear()
            r.advance(15 * 60)
            r.monitor.tick()
            self.assertEqual(r.unit("ETHUSDT/15m").status, UnitStatus.OK)
        finally:
            r.close()

    def test_persistence_failure_is_reported_and_retried_without_duplicates(self):
        r = Rig()
        try:
            real = r.monitor.regime.store
            fails = {"n": 2}

            class Flaky:
                def save(self, s):
                    if fails["n"]:
                        fails["n"] -= 1
                        raise StorageError("disk I/O error")
                    return real.save(s)
            r.monitor.regime.store = Flaky()
            r.monitor.tick()
            self.assertEqual(set(statuses(r).values()), {UnitStatus.PERSISTENCE_ERROR})
            self.assertEqual(r.monitor.health.snapshots_created, 0)
            ev = r.runs.events(r.monitor.health.run_id)
            self.assertEqual({e["persisted"] for e in ev}, {"failed"})
            r.advance(30)
            r.monitor.tick()
            self.assertEqual(set(statuses(r).values()), {UnitStatus.OK})
            self.assertEqual(rows(r, "regime_snapshots"), 2)
        finally:
            r.close()

    def test_unknown_symbol_is_disabled_not_retried(self):
        r = Rig(mcfg(symbols=["ETHUSDT", "NOPEUSDT"]), unknown={"NOPEUSDT"})
        try:
            r.monitor.tick()
            self.assertEqual(r.unit("NOPEUSDT/15m").status, UnitStatus.API_ERROR)
            self.assertEqual(r.unit("ETHUSDT/15m").status, UnitStatus.OK)
            for _ in range(4):
                r.advance(15 * 60)
                r.monitor.tick()
            self.assertEqual(r.ex.kline_calls.count(("NOPEUSDT", "15")), 0)
        finally:
            r.close()


class MonitorIdempotencyTests(unittest.TestCase):
    def test_hour_of_polling_creates_one_snapshot_per_bar(self):
        r = Rig()
        try:
            for _ in range(121):                          # 12:00:30 .. 13:00:30, poll every 30 s
                r.monitor.tick()
                r.advance(30)
            n15 = len(r.regime_store.history(Symbol("ETHUSDT"), M15, 100))
            n1h = len(r.regime_store.history(Symbol("ETHUSDT"), H1, 100))
            self.assertEqual((n15, n1h), (5, 2))          # 12:00..13:00 closes; 12:00, 13:00
            self.assertEqual(rows(r, "regime_snapshots"), 7)
            self.assertEqual(len(r.runs.events(r.monitor.health.run_id)), 7)
            self.assertEqual(r.monitor.health.snapshots_duplicate, 0)
        finally:
            r.close()

    def test_restart_uses_local_data_and_creates_no_duplicates(self):
        r1 = Rig()
        r1.monitor.tick()
        r1.market.writer.conn.close()
        try:
            r2 = Rig(db_dir=r1.dir, exchange=r1.ex)
            calls = len(r1.ex.kline_calls)
            r2.monitor.tick()
            self.assertEqual(len(r1.ex.kline_calls), calls)             # history complete locally
            self.assertEqual(set(statuses(r2).values()), {UnitStatus.OK})
            self.assertEqual((r2.monitor.health.snapshots_created, r2.monitor.health.snapshots_duplicate), (0, 2))
            self.assertEqual(rows(r2, "regime_snapshots"), 2)
            self.assertEqual(rows(r2, "monitor_runs"), 2)
            r2.close()
        finally:
            import shutil
            shutil.rmtree(r1.dir, ignore_errors=True)


class MonitorLifecycleTests(unittest.TestCase):
    def test_limited_runtime(self):
        r = Rig()
        try:
            h = r.monitor.run(timedelta(minutes=20))
            self.assertEqual(h.status, RunStatus.COMPLETED)
            self.assertGreaterEqual(r.clock.now() - START, timedelta(minutes=20))
            self.assertEqual(h.cycles_total, 41)
            run = r.runs.run(h.run_id)
            self.assertEqual(run["status"], "completed")
            self.assertEqual(run["summary"]["snapshots_created"], 3)
        finally:
            r.close()

    def test_graceful_stop_request(self):
        r = Rig()
        try:
            r.time.hooks[3] = r.monitor.request_stop
            h = r.monitor.run()
            self.assertEqual((h.status, h.cycles_total), (RunStatus.STOPPED, 3))
            self.assertEqual(r.runs.run(h.run_id)["status"], "stopped")
            self.assertIsNotNone(r.runs.run(h.run_id)["finished_at"])
        finally:
            r.close()

    def test_stop_via_cli_flag(self):
        r = Rig()
        try:
            r.time.hooks[2] = lambda: r.runs.request_stop() and False
            h = r.monitor.run()
            self.assertEqual((h.status, h.cycles_total), (RunStatus.STOPPED, 3))
        finally:
            r.close()

    def test_forced_interrupt_still_finalises(self):
        r = Rig()
        try:
            def boom():
                raise KeyboardInterrupt
            r.time.hooks[2] = boom
            h = r.monitor.run()
            run = r.runs.run(h.run_id)
            self.assertEqual((h.status, run["status"]), (RunStatus.STOPPED, "stopped"))
            self.assertEqual(run["summary"]["cycles"], 2)
            self.assertEqual(rows(r, "regime_snapshots"), 2)
        finally:
            r.close()

    def test_sigint_handler_requests_graceful_stop(self):
        from app.cli.monitor_cmds import run_monitor
        import signal
        r = Rig()
        try:
            r.time.hooks[2] = lambda: signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            summary = run_monitor(r.monitor, None)
            self.assertEqual(summary["status"], "stopped")
            self.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)   # restored
        finally:
            r.close()

    def test_heartbeat_and_health_state(self):
        r = Rig()
        try:
            r.time.hooks[6] = r.monitor.request_stop      # ~3 minutes, heartbeat every 60 s
            h = r.monitor.run()
            run = r.runs.run(h.run_id)
            d = run["health"]
            for k in ("run_id", "status", "uptime_s", "cycles_total", "cycles_with_errors", "last_cycle_at",
                      "last_successful_cycle_at", "last_api_success_at", "api_status", "last_snapshot_saved_at",
                      "snapshots_created", "stale_units", "unavailable_units", "units", "outcome_counts"):
                self.assertIn(k, d)
            self.assertEqual(d["uptime_s"], 180.0)
            self.assertEqual(d["api_status"], "ok")
            self.assertGreaterEqual(d["outcome_counts"]["no_new_bar"], 8)
            self.assertIsNotNone(run["last_heartbeat_at"])
        finally:
            r.close()

    def test_nothing_runs_on_import(self):
        import importlib
        import app.monitor.runtime as rt
        importlib.reload(rt)
        self.assertFalse(any(isinstance(v, rt.MarketMonitor) for v in vars(rt).values()))


class MonitorCliTests(unittest.TestCase):
    def test_health_and_last_commands(self):
        r = Rig()
        try:
            r.time.hooks[2] = r.monitor.request_stop
            r.monitor.run()
            from app.cli.main import main
            env = {"DB_PATH": str(r.dir / "mon.sqlite3")}
            out = io.StringIO()
            with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, env):
                self.assertEqual(main(["monitor", "health"]), 0)
                self.assertEqual(main(["monitor", "last", "--symbol", "ETHUSDT", "--tf", "15m"]), 0)
                self.assertEqual(main(["monitor", "stop"]), 0)
            text = out.getvalue()
            health = json.loads(text[:text.index("\nETHUSDT")])
            self.assertEqual(health["status"], "stopped")
            self.assertIn("ETHUSDT 15m @ 2026-03-02T12:00:00+00:00", text)
            self.assertIn("no running monitor", text)
        finally:
            r.close()

    def test_run_arguments(self):
        from app.cli.main import build_parser
        a = build_parser().parse_args(["monitor", "run", "--hours", "2", "--symbols", "BTCUSDT", "--tf", "15m"])
        self.assertEqual((a.hours, a.symbols, a.tf), (2, ["BTCUSDT"], ["15m"]))


class MonitorConfigTests(unittest.TestCase):
    def test_default_file(self):
        c = MonitorConfig.from_file(ROOT / "config" / "monitor_v1.json")
        self.assertEqual([s.name for s in c.symbols], ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
        self.assertEqual([t.value for t in c.timeframes], ["15m", "1h", "4h"])

    def test_no_hidden_defaults_and_validation(self):
        raw = json.loads((ROOT / "config" / "monitor_v1.json").read_text())
        for k in [k for k in raw if not k.startswith("_") and k != "schema"]:
            with self.subTest(missing=k), self.assertRaises(DomainError):
                MonitorConfig.from_mapping({x: y for x, y in raw.items() if x != k})
        for bad in ({"poll_interval_s": 0}, {"poll_interval_s": 1200}, {"unit_backoff_max_s": 1},
                    {"symbols": []}, {"timeframes": ["15"]}, {"max_runtime_minutes": 0}, {"heartbeat_interval_s": 1.5}):
            with self.subTest(bad=bad), self.assertRaises(DomainError):
                MonitorConfig.from_mapping({**raw, **bad})

    def test_backoff_bounded(self):
        c = mcfg()
        self.assertEqual([c.backoff_s(n) for n in (1, 2, 3, 4, 5, 50)], [5, 10, 20, 40, 60, 60])

    def test_no_magic_numbers_in_runtime(self):
        """Behaviour-relevant numbers come from MonitorConfig. The only literals allowed
        are 0/1 and module-level NAMED constants (unit conversion, text limits)."""
        tree = ast.parse((ROOT / "app" / "monitor" / "runtime.py").read_text())
        named = {id(n.value) for n in tree.body if isinstance(n, ast.Assign)
                 and all(isinstance(t, ast.Name) and t.id.isupper() or t.id.startswith("_") and t.id[1:].isupper()
                         for t in n.targets)}
        nums = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and id(n) not in named
                and isinstance(n.value, (int, float)) and not isinstance(n.value, bool) and n.value not in (0, 1)]
        self.assertEqual(nums, [])


class MonitorSafetyTests(unittest.TestCase):
    def test_no_orders_no_llm_no_private_api(self):
        root = ROOT / "app" / "monitor"
        banned = {"submit", "place_order", "cancel", "ExecutionGateway", "DisabledGateway", "anthropic", "openai",
                  "gemini", "ollama", "api_key", "bybit_api_key", "bybit_api_secret", "telegram"}
        for f in root.rglob("*.py"):
            tree = ast.parse(f.read_text())
            ids = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
                  {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | \
                  {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
            self.assertEqual(ids & banned, set(), f.name)

    def test_monitor_uses_only_public_endpoints(self):
        r = Rig()
        try:
            r.monitor.tick()
            from app.data.bybit.client import ALLOWED_ENDPOINTS
            self.assertTrue(all(e.startswith("/v5/market/") for e in ALLOWED_ENDPOINTS))
            self.assertFalse(r.settings.bybit_api_key.is_set())
        finally:
            r.close()

    def test_history_is_not_altered(self):
        r = Rig()
        try:
            r.monitor.tick()
            for sql in ("UPDATE monitor_events SET status='ok'", "DELETE FROM monitor_events",
                        "DELETE FROM regime_snapshots", "UPDATE candles SET close='1'"):
                with self.assertRaises(sqlite3.DatabaseError):
                    r.market.writer.conn.execute(sql)
        finally:
            r.close()
