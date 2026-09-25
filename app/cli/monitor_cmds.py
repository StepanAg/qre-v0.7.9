"""`python -m app monitor ...` - composition root of the market monitor.
Public market data only: no API keys, no orders, no LLM."""
from __future__ import annotations

import argparse
import json
import signal
import sys
from datetime import datetime, timedelta, timezone

from app.config.settings import PROJECT_ROOT, load_settings
from app.core.clock import SystemClock
from app.core.logging import add_file_handler, setup_logging
from app.domain.market import Symbol, Timeframe
from app.monitor.config import MonitorConfig
from app.monitor.runtime import MarketMonitor
from app.research.regime.config import RegimeConfig
from app.research.regime.service import RegimeService
from app.research.service import FeatureService
from app.storage.database import bootstrap
from app.storage.monitor import SQLiteMonitorRunStore
from app.storage.regime import SQLiteRegimeSnapshotStore
from app.storage.writer import SerializedWriter


def build_monitor(settings, cfg: MonitorConfig, *, transport=None, clock=None, waiter=None,
                  regime_config: RegimeConfig | None = None, structure: bool = True, setups: bool = True):
    from app.cli.data_cmds import build as build_data
    backfill, market, _ = build_data(settings, transport)       # existing Phase 1 wiring (public REST)
    w = market.writer
    if clock is not None:
        backfill.clock = backfill.provider.clock = clock
    regime = RegimeService(FeatureService(market), regime_config or RegimeConfig.from_file(settings.regime_config_file),
                           SQLiteRegimeSnapshotStore(w))
    runs = SQLiteMonitorRunStore(w)
    structure_svc = None
    if structure:
        from app.research.structure.config import StructureConfig
        from app.research.structure.service import StructureService
        from app.storage.structure import SQLiteStructureStore
        structure_svc = StructureService(market, StructureConfig.from_file(settings.structure_config_file),
                                         SQLiteStructureStore(w))
    setup_svc = None
    if setups:
        from app.research.setup.config import SetupConfig
        from app.research.setup.service import SetupService
        from app.research.structure.config import StructureConfig
        from app.research.structure.service import StructureService
        from app.storage.setup import SQLiteSetupStore
        base_structure = structure_svc or StructureService(
            market, StructureConfig.from_file(settings.structure_config_file))
        setup_svc = SetupService(market, base_structure, SetupConfig.from_file(settings.setup_config_file),
                                 SQLiteSetupStore(w))
    return MarketMonitor(cfg, backfill, market, regime, runs, clock or SystemClock(), waiter,
                         structure=structure_svc, setups=setup_svc), runs


def _stores(settings):
    w = SerializedWriter(bootstrap(settings.db_path, shared=True))
    return SQLiteMonitorRunStore(w), SQLiteRegimeSnapshotStore(w)


def run_monitor(monitor: MarketMonitor, max_runtime: timedelta | None) -> dict:
    """Runs until max runtime / stop request / Ctrl+C. First Ctrl+C = graceful stop,
    second Ctrl+C = immediate KeyboardInterrupt (still finalises run state)."""
    def on_sigint(signum, frame):
        if monitor._stop_flag:
            raise KeyboardInterrupt
        print("\nstopping after the current operation... (Ctrl+C again to force)", file=sys.stderr, flush=True)
        monitor.request_stop()

    previous = signal.getsignal(signal.SIGINT)
    try:
        signal.signal(signal.SIGINT, on_sigint)
    except ValueError:                      # not in the main thread (tests)
        previous = None
    try:
        monitor.run(max_runtime)
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)
    return monitor.summary()


def cmd(a: argparse.Namespace) -> int:
    s = load_settings()
    if a.mon_cmd == "supervise":
        return _supervise(s, a)
    if a.mon_cmd == "stop":
        runs, _ = _stores(s)
        ids = runs.request_stop(a.run_id)
        print(f"stop requested for: {ids or 'no running monitor'}")
        return 0
    if a.mon_cmd == "health":
        runs, _ = _stores(s)
        r = runs.run(a.run_id) if a.run_id else runs.latest_run()
        if r is None:
            print("no monitor runs recorded")
            return 1
        out = {k: r.get(k) for k in ("run_id", "status", "started_at", "finished_at", "last_heartbeat_at")}
        if r.get("last_heartbeat_at") and not r.get("finished_at"):
            age = datetime.now(timezone.utc) - datetime.fromisoformat(r["last_heartbeat_at"])
            out["heartbeat_age_s"] = round(age.total_seconds(), 1)
        out["health"] = r.get("health")
        out["summary"] = r.get("summary")
        print(json.dumps(out, indent=2, default=str))
        return 0
    if a.mon_cmd == "last":
        _, store = _stores(s)
        tfs = [Timeframe.parse(a.tf)] if a.tf else [Timeframe.parse(t) for t in ("15m", "1h", "4h")]
        for tf in tfs:
            h = store.history(Symbol(a.symbol), tf, 1)
            if not h:
                print(f"{a.symbol} {tf.value}: no regime snapshot yet")
                continue
            r = h[0]
            print(f"{a.symbol} {tf.value} @ {r.as_of.isoformat()}: {r.regime.value} "
                  f"(trend {r.trend_direction.value}/{r.trend_strength.value}, vol {r.volatility_state.value}, "
                  f"mtf {r.mtf_alignment.value}, btc {r.btc_context.status.value if r.btc_context else '-'}, "
                  f"data {r.data_quality.value})")
            if a.json:
                print(r.to_json())
            from app.storage.structure import SQLiteStructureStore
            st = SQLiteStructureStore(store.writer).latest(Symbol(a.symbol), tf)
            if st is not None:
                print(f"  structure @ {st.as_of.isoformat()}: {st.direction.value} (data {st.data_quality.value}, "
                      f"active liquidity {len(st.liquidity)}, events in window {len(st.events)})")
        return 0
    # ---------------------------------------------------------------- run
    cfg = MonitorConfig.from_file(a.config or s.monitor_config_file).with_overrides(
        symbols=a.symbols, timeframes=a.tf)
    max_runtime = None
    if a.minutes or a.hours:
        max_runtime = timedelta(minutes=(a.minutes or 0) + 60 * (a.hours or 0))
    setup_logging(s.log_level)
    log_file = a.log_file or PROJECT_ROOT / "var" / "logs" / f"monitor-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.log"
    add_file_handler(log_file)
    monitor, runs = build_monitor(s, cfg, structure=not a.no_structure, setups=not a.no_setups)
    abandoned = runs.mark_abandoned(datetime.now(timezone.utc))
    if abandoned:
        print(f"previous run(s) did not finish cleanly, marked abandoned: {abandoned}")
    print(f"monitor {monitor.health.run_id}: {[x.name for x in cfg.symbols]} x {[t.value for t in cfg.timeframes]}, "
          f"runtime={'until Ctrl+C' if not max_runtime and not cfg.max_runtime_minutes else max_runtime or f'{cfg.max_runtime_minutes} min'}, "
          f"log={log_file}")
    summary = run_monitor(monitor, max_runtime)
    print("summary:", json.dumps(summary, indent=2))
    return 0 if summary["status"] in ("stopped", "completed") else 3


def child_args(a: argparse.Namespace) -> list[str]:
    """Arguments the supervisor passes on to `monitor run`."""
    child: list[str] = []
    if a.symbols:
        child += ["--symbols", *a.symbols]
    if a.tf:
        child += ["--tf", *a.tf]
    if a.config:
        child += ["--config", a.config]
    if a.no_structure:
        child.append("--no-structure")
    if a.no_setups:
        child.append("--no-setups")
    return child


def _supervise(s, a: argparse.Namespace) -> int:
    from app.cli.keepawake import keep_awake
    from app.cli.supervisor import Supervisor, SupervisorPolicy, db_heartbeat_reader
    setup_logging(s.log_level)
    add_file_handler(PROJECT_ROOT / "var" / "logs" / "supervisor.log")
    deadline = None
    if a.minutes or a.hours:
        deadline = datetime.now(timezone.utc) + timedelta(minutes=(a.minutes or 0) + 60 * (a.hours or 0))
    child = child_args(a)
    policy = SupervisorPolicy(hang_after_s=a.hang_after_min * 60)
    sup = Supervisor(child, deadline=deadline, heartbeat=db_heartbeat_reader(s.db_path), policy=policy)
    with keep_awake(not a.allow_sleep) as sleep_state:
        print(f"supervisor: until {deadline.isoformat() if deadline else 'Ctrl+C'}; sleep: {sleep_state}; "
              f"restarts on crash/hang, internet outages are handled by the monitor itself")
        report = sup.run()
    print("supervisor summary:", json.dumps(report.summary(), indent=2))
    return 0 if report.outcome in ("completed", "stopped") else 4


def register(sub) -> None:
    m = sub.add_parser("monitor", help="market monitor runtime (public data, no orders)")
    ms = m.add_subparsers(dest="mon_cmd", required=True)
    r = ms.add_parser("run", help="run the monitor (Ctrl+C = graceful stop)")
    r.add_argument("--minutes", type=int, help="limited run, e.g. --minutes 30")
    r.add_argument("--hours", type=int, help="limited run, e.g. --hours 2")
    r.add_argument("--symbols", nargs="*", help="override config symbols")
    r.add_argument("--tf", nargs="*", help="override config timeframes")
    r.add_argument("--config", help="monitor config JSON (default: MONITOR_CONFIG_FILE)")
    r.add_argument("--log-file")
    r.add_argument("--no-structure", action="store_true", help="skip the Phase 5 structure step (Phase 4 behaviour)")
    r.add_argument("--no-setups", action="store_true", help="skip the Phase 6 setup detection step")
    h = ms.add_parser("health", help="runtime health of the latest (or given) run")
    h.add_argument("--run-id")
    l = ms.add_parser("last", help="latest stored regime snapshot")
    l.add_argument("--symbol", required=True)
    l.add_argument("--tf")
    l.add_argument("--json", action="store_true")
    sv = ms.add_parser("supervise", help="run the monitor under a watchdog: restart on crash/hang, keep PC awake")
    sv.add_argument("--minutes", type=int)
    sv.add_argument("--hours", type=int, help="total run time, e.g. --hours 24")
    sv.add_argument("--symbols", nargs="*")
    sv.add_argument("--tf", nargs="*")
    sv.add_argument("--config")
    sv.add_argument("--no-structure", action="store_true")
    sv.add_argument("--no-setups", action="store_true")
    sv.add_argument("--hang-after-min", type=int, default=10, help="restart if no heartbeat for N minutes")
    sv.add_argument("--allow-sleep", action="store_true", help="do not prevent the computer from sleeping")
    st = ms.add_parser("stop", help="ask a running monitor (other terminal) to stop gracefully")
    st.add_argument("--run-id")
    m.set_defaults(fn=cmd)
