"""Supervisor: keeps `python -m app monitor run` alive during long local runs.

It watches the monitor from OUTSIDE (a separate process), so it survives anything
that happens inside it:
  * crash / non-zero exit   -> restart with growing pause (5 s .. 5 min)
  * hang (no heartbeat)     -> kill and restart
  * config/safety error     -> exit code 2 -> NOT restarted (repeating cannot fix it)
  * graceful end (exit 0)   -> finished (Ctrl+C, `monitor stop`, time limit reached)
  * too many restarts       -> gives up and says so (no infinite crash loop)
Internet outages are handled INSIDE the monitor (retry/backoff, catch-up of missed
bars); they are not a reason to restart the process.
"""
from __future__ import annotations

import math
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence

from app.core.logging import get_logger

log = get_logger("supervisor")

from app.cli.main import EXIT_CONFIG_ERROR   # single source of truth for exit codes  # noqa: E402

EXIT_OK = 0


@dataclass(frozen=True)
class SupervisorPolicy:
    backoff_base_s: float = 5          # pause before restart n: base * 2^(n-1) ...
    backoff_max_s: float = 300         # ... capped at 5 minutes
    stable_after_s: float = 600        # a child that ran this long resets the backoff
    max_restarts_per_hour: int = 20    # crash-loop guard
    hang_after_s: float = 600          # no heartbeat for this long -> hung (heartbeat is every 60 s)
    startup_grace_s: float = 600       # first heartbeat may take a while (history warm-up)
    check_interval_s: float = 15       # how often the child is checked
    graceful_timeout_s: float = 60     # wait for a clean shutdown before killing

    def backoff(self, n: int) -> float:
        return min(self.backoff_max_s, self.backoff_base_s * 2 ** (max(n, 1) - 1))


@dataclass
class SupervisorReport:
    started_at: datetime
    finished_at: datetime | None = None
    launches: int = 0
    restarts: int = 0
    reasons: list[str] = field(default_factory=list)
    outcome: str = "running"

    def summary(self) -> dict:
        return {"outcome": self.outcome, "launches": self.launches, "restarts": self.restarts,
                "restart_reasons": self.reasons,
                "wall_time_s": round(((self.finished_at or datetime.now(timezone.utc)) - self.started_at)
                                     .total_seconds(), 1)}


HeartbeatReader = Callable[[datetime], "datetime | None"]


def db_heartbeat_reader(db_path: Path) -> HeartbeatReader:
    """Latest heartbeat (or start time) of a monitor run started at/after `since` (SQL lives in storage)."""
    from app.storage.monitor import read_latest_heartbeat
    return lambda since: read_latest_heartbeat(db_path, since)


class Supervisor:
    def __init__(self, child_args: Sequence[str], *, deadline: datetime | None, heartbeat: HeartbeatReader,
                 policy: SupervisorPolicy = SupervisorPolicy(),
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 sleep: Callable[[float], None] = time.sleep,
                 launcher: Callable[[list[str]], subprocess.Popen] | None = None) -> None:
        self.child_args = list(child_args)
        self.deadline = deadline
        self.heartbeat = heartbeat
        self.p = policy
        self.now = now
        self.sleep = sleep
        self.launch = launcher or (lambda cmd: subprocess.Popen(cmd))
        self.report = SupervisorReport(now())
        self._restart_times: list[datetime] = []
        self._stop = False
        self.child: subprocess.Popen | None = None

    def command(self) -> list[str]:
        cmd = [sys.executable, "-m", "app", "monitor", "run", *self.child_args]
        if self.deadline is not None:
            remaining = (self.deadline - self.now()).total_seconds()
            cmd += ["--minutes", str(max(1, math.ceil(remaining / 60)))]
        return cmd

    def request_stop(self) -> None:
        self._stop = True

    def run(self) -> SupervisorReport:
        rep = self.report
        consecutive = 0
        try:
            while not self._stop:
                if self.deadline is not None and self.now() >= self.deadline:
                    rep.outcome = "completed"
                    break
                started = self.now()
                rep.launches += 1
                cmd = self.command()
                log.info("launch #%d: %s", rep.launches, " ".join(cmd[1:]))
                self.child = self.launch(cmd)
                code, reason = self._watch(self.child, started)
                ran = (self.now() - started).total_seconds()
                if code == EXIT_OK and reason is None:
                    rep.outcome = "completed" if self.deadline and self.now() >= self.deadline else "stopped"
                    break
                if code == EXIT_CONFIG_ERROR:
                    rep.outcome = "config_error"
                    rep.reasons.append("exit code 2 (configuration/safety error) - not restarted")
                    log.error("monitor refused to start (exit 2): fix the configuration; not restarting")
                    break
                if self._stop:
                    rep.outcome = "stopped"
                    break
                why = reason or f"exit code {code}"
                consecutive = 1 if ran >= self.p.stable_after_s else consecutive + 1
                cutoff = self.now() - timedelta(hours=1)
                self._restart_times = [t for t in self._restart_times if t > cutoff]
                if len(self._restart_times) >= self.p.max_restarts_per_hour:
                    rep.outcome = "gave_up"
                    rep.reasons.append(f"{why}: more than {self.p.max_restarts_per_hour} restarts in 1 h - giving up")
                    log.error("crash loop: %d restarts within an hour, giving up", len(self._restart_times))
                    break
                pause = self.p.backoff(consecutive)
                rep.restarts += 1
                rep.reasons.append(f"{why} after {int(ran)} s -> restart in {int(pause)} s")
                self._restart_times.append(self.now())
                log.warning("monitor ended unexpectedly (%s after %d s); restarting in %d s", why, ran, pause)
                self.sleep(pause)
        except KeyboardInterrupt:
            rep.outcome = "stopped"
            self._shutdown_child()
        rep.finished_at = self.now()
        log.info("supervisor finished: %s", rep.summary())
        return rep

    def _watch(self, proc: subprocess.Popen, started: datetime) -> tuple[int | None, str | None]:
        """Wait for the child; returns (exit code, restart reason or None)."""
        while True:
            try:
                return proc.wait(timeout=self.p.check_interval_s), None
            except subprocess.TimeoutExpired:
                pass
            except KeyboardInterrupt:
                self._stop = True
                self._shutdown_child()
                return proc.returncode, None
            if self._stop:
                self._shutdown_child()
                return proc.returncode, None
            now = self.now()
            hb = self.heartbeat(started)
            silent_for = (now - (hb or started)).total_seconds()
            limit = self.p.hang_after_s if hb else self.p.startup_grace_s
            if silent_for > limit:
                log.error("no heartbeat for %d s (limit %d s): monitor considered hung, killing it",
                          silent_for, limit)
                self._kill(proc)
                return proc.returncode, f"hung (no heartbeat for {int(silent_for)} s)"

    def _shutdown_child(self) -> None:
        proc = self.child
        if proc is None or proc.poll() is not None:
            return
        try:                                       # the child got Ctrl+C from the console itself
            proc.wait(timeout=self.p.graceful_timeout_s)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            self._kill(proc)

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
