"""MarketMonitor: the runtime loop.

tick():
  for every unit (symbol, base timeframe):
    expected bar = last bar closed at (now - grace)
    already processed?            -> NO_NEW_BAR (nothing fetched, nothing analysed, not logged)
    in backoff?                   -> skipped until next_attempt_at
    sync data series              -> base series (blocking), higher TFs + BTC (non-blocking)
    expected bar not stored yet?  -> WAITING_FOR_BAR, after stale_after_s -> STALE
    Feature Engine -> Regime Engine -> classify outcome -> persist (idempotent)
run(): start -> tick every poll_interval_s -> heartbeat -> stop / max runtime -> finish.

All timing comes from MonitorConfig; all time from the injected clock; waiting
goes through an injected waiter (interruptible), so the loop is fully testable
offline and never busy-loops.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

import app
from app.core.clock import Clock
from app.core.errors import StorageError
from app.core.ids import new_id
from app.core.logging import get_logger, log_context
from app.data.backfill import BackfillService
from app.data.errors import BybitApiError, HttpStatusError, MalformedResponse, MarketDataError, PaginationError
from app.data.ports import MarketDataStore
from app.domain.enums import Category, QualityStatus
from app.domain.market import Symbol, Timeframe
from app.domain.regime import RegimeSnapshot
from app.monitor.config import MonitorConfig
from app.monitor.health import (FAILURE_STATUSES, RETRYABLE_STATUSES, SETUP_RETRYABLE, STRUCTURE_RETRYABLE, ApiStatus,
                                HealthState, RunStatus, SetupStepStatus, StructureStatus, UnitState, UnitStatus)
from app.monitor.ports import MonitorRunStore
from app.research.regime.service import RegimeService
from app.research.setup.service import SetupService
from app.research.structure.service import StructureService

log = get_logger("monitor")

_WARMUP_QUALITIES = {QualityStatus.INSUFFICIENT_HISTORY, QualityStatus.MISSING_HISTORY}
_MS_PER_S = 1000              # unit conversion, not a tunable
_ERROR_TEXT_LIMIT = 300       # max chars of an error message kept in health state


class UnitFailure(Exception):
    def __init__(self, status: UnitStatus, detail: str) -> None:
        self.status, self.detail = status, detail
        super().__init__(f"{status.value}: {detail}")


def classify_data_error(e: BaseException) -> UnitStatus:
    """Map ingestion errors onto runtime statuses (never a generic failure)."""
    if isinstance(e, StorageError):
        return UnitStatus.PERSISTENCE_ERROR
    if isinstance(e, (MalformedResponse, PaginationError)):
        return UnitStatus.INVALID_DATA
    if isinstance(e, MarketDataError):
        if getattr(e, "retryable", False) or type(e).__name__ == "RetriesExhausted":
            return UnitStatus.API_TEMPORARY_ERROR
        if isinstance(e, (BybitApiError, HttpStatusError)):
            return UnitStatus.API_ERROR
        return UnitStatus.API_TEMPORARY_ERROR
    return UnitStatus.INTERNAL_ERROR


@dataclass
class SeriesState:
    synced_through: datetime | None = None   # close time of the newest bar known to be stored
    warmup_done: bool = False


@dataclass
class CycleSummary:
    cycle_id: str
    at: datetime
    processed: dict[str, str] = field(default_factory=dict)     # unit -> status of units actually processed
    skipped_backoff: int = 0
    no_new_bar: int = 0
    errors: int = 0


class MarketMonitor:
    def __init__(self, config: MonitorConfig, backfill: BackfillService, market: MarketDataStore,
                 regime: RegimeService, runs: MonitorRunStore, clock: Clock,
                 waiter: Callable[[float], bool] | None = None,
                 structure: StructureService | None = None, setups: SetupService | None = None) -> None:
        if regime.store is None:
            raise ValueError("RegimeService needs a snapshot store for the monitor")
        self.cfg = config
        self.backfill = backfill
        self.market = market
        self.regime = regime
        self.runs = runs
        self.clock = clock
        self._stop_flag = False
        self._started = False
        self._waiter = waiter
        self.required_bars = regime.bars_per_value()        # single source: feature requirements
        self.tfs = config.timeframes
        self.btc = regime.config.btc_symbol
        self.btc_tf = regime.config.btc_timeframe
        now = clock.now()
        self.health = HealthState(new_id("evt"), now, now)
        self.units = [UnitState(s.name, tf.value) for s in config.symbols for tf in self.tfs]
        self.health.units = {u.key: u for u in self.units}
        if structure is not None and structure.store is None:
            raise ValueError("StructureService needs a store for the monitor")
        self.structure = structure
        self.health.structure_enabled = structure is not None
        if setups is not None and setups.store is None:
            raise ValueError("SetupService needs a store for the monitor")
        self.setups = setups
        self.health.setup_enabled = setups is not None
        self._last_regime: dict[str, RegimeSnapshot] = {}      # regime context for setup-only retries
        self.series: dict[tuple[str, Timeframe], SeriesState] = {}
        self.instruments_synced = False
        self.disabled_symbols: dict[str, str] = {}
        self._last_heartbeat: datetime | None = None
        self._grace = timedelta(seconds=config.bar_close_grace_s)
        self._stale_after = timedelta(seconds=config.stale_after_s)

    # ================================================================ control
    def request_stop(self) -> None:
        """Graceful: the current unit finishes, then the loop exits."""
        self._stop_flag = True

    def _should_stop(self) -> bool:
        if self._stop_flag:
            return True
        try:
            if self.runs.stop_requested(self.health.run_id):
                log.info("stop requested via CLI (monitor stop)")
                self._stop_flag = True
        except StorageError as e:
            log.error("cannot read stop flag: %s", e)
        return self._stop_flag

    def _wait(self, seconds: float) -> bool:
        if self._waiter is not None:
            return self._waiter(seconds) or self._stop_flag
        deadline = time.monotonic() + seconds
        while not self._stop_flag and time.monotonic() < deadline:
            time.sleep(min(1, max(0, deadline - time.monotonic())))   # 1 s granularity -> quick Ctrl+C
        return self._stop_flag

    # ================================================================== run
    def start(self, max_runtime: timedelta | None = None) -> None:
        """Register the run (idempotent). tick() calls it if needed."""
        if self._started:
            return
        h = self.health
        self.runs.start_run(h.run_id, h.started_at, self.cfg.canonical(), {
            "code_version": app.__version__, "monitor_config_hash": self.cfg.config_hash,
            "regime_config_hash": self.regime.config.config_hash, "required_bars": self.required_bars,
            "max_runtime_s": max_runtime.total_seconds() if max_runtime else None})
        self._started = True
        h.status = RunStatus.RUNNING
        log.info("monitor %s started: %d symbols x %s, required bars/series=%d, poll=%ss", h.run_id,
                 len(self.cfg.symbols), [t.value for t in self.tfs], self.required_bars, self.cfg.poll_interval_s)

    def finish(self, status: RunStatus) -> dict:
        h = self.health
        h.status = status
        h.now = self.clock.now()
        summary = self.summary()
        try:
            self.runs.finish_run(h.run_id, h.now, h.status.value, h.to_dict(), summary)
        except StorageError as e:
            log.error("could not persist final run state: %s", e)
        log.info("monitor %s %s: %s", h.run_id, h.status.value, summary)
        return summary

    def run(self, max_runtime: timedelta | None = None) -> HealthState:
        h = self.health
        if max_runtime is None and self.cfg.max_runtime_minutes is not None:
            max_runtime = timedelta(minutes=self.cfg.max_runtime_minutes)
        self.start(max_runtime)
        final = RunStatus.STOPPED        # graceful stop (Ctrl+C, stop request) unless changed below
        crashes = 0
        try:
            while True:
                try:
                    self.tick()
                    crashes = 0
                except KeyboardInterrupt:
                    raise
                except Exception as e:                       # a whole-cycle crash (not a unit failure)
                    crashes += 1
                    h.cycles_with_errors += 1
                    log.exception("cycle crashed (%d in a row): %s", crashes, e)
                    if crashes >= self.cfg.unit_max_attempts:
                        final = RunStatus.FAILED
                        break
                if self._should_stop():
                    break
                if max_runtime is not None and self.clock.now() - h.started_at >= max_runtime:
                    final = RunStatus.COMPLETED
                    break
                if self._wait(self.cfg.poll_interval_s):
                    break
        except KeyboardInterrupt:
            log.info("interrupted (forced)")
        finally:
            self.finish(final)
        return h

    def summary(self) -> dict:
        h = self.health
        return {"run_id": h.run_id, "status": h.status.value, "uptime_s": round(h.uptime_s, 1),
                "cycles": h.cycles_total, "cycles_with_errors": h.cycles_with_errors,
                "analyses": h.analyses_run, "snapshots_created": h.snapshots_created,
                "snapshots_duplicate": h.snapshots_duplicate, "api_requests": h.api_requests,
                "api_status": h.api_status.value, "outcomes": dict(sorted(h.outcome_counts.items()))}

    # ================================================================= tick
    def tick(self) -> CycleSummary:
        self.start()
        h = self.health
        now = self.clock.now()
        cyc = CycleSummary(new_id("cor"), now)
        h.cycles_total += 1
        h.now = now
        with log_context(correlation_id=cyc.cycle_id):
            if not self.instruments_synced:
                self._sync_instruments()
            for u in self.units:
                if self._stop_flag:
                    break
                self._visit(u, now, cyc)
        h.last_cycle_at = now
        if cyc.errors:
            h.cycles_with_errors += 1
        else:
            h.last_successful_cycle_at = now
        m = self.backfill.metrics
        h.api_requests, h.api_retries = m.api_requests, m.retries
        self._maybe_heartbeat(now)
        return cyc

    def _visit(self, u: UnitState, now: datetime, cyc: CycleSummary) -> None:
        tf = Timeframe.parse(u.timeframe)
        expected = tf.floor(now - self._grace)
        if u.symbol in self.disabled_symbols:
            return
        if u.last_processed_as_of == expected:
            pending = False
            if self._structure_pending(u, expected, now):          # regime done, structure step still owed
                self._process_structure(u, Symbol(u.symbol), tf, expected, now, cyc)
                pending = True
            if self._setup_pending(u, expected):                    # regime done, setup step still owed
                self._process_setup(u, Symbol(u.symbol), tf, expected, self._last_regime.get(u.key), cyc)
                pending = True
            if pending:
                return
            cyc.no_new_bar += 1
            self.health.count(UnitStatus.NO_NEW_BAR)
            return
        if u.next_attempt_at is not None and now < u.next_attempt_at:
            cyc.skipped_backoff += 1
            return
        t0 = time.perf_counter()
        status, snap, persisted, detail = self._process(u, Symbol(u.symbol), tf, expected, now)
        dur_ms = round((time.perf_counter() - t0) * _MS_PER_S, 1)
        self._apply(u, status, expected, now, snap, detail)
        cyc.processed[u.key] = status.value
        if status in FAILURE_STATUSES:
            cyc.errors += 1
        self.health.count(status)
        event = {"cycle_id": cyc.cycle_id, "symbol": u.symbol, "timeframe": u.timeframe,
                 "as_of": expected, "status": status.value,
                 "regime": snap.regime.value if snap else None,
                 "data_quality": snap.data_quality.value if snap else None,
                 "persisted": persisted, "duration_ms": dur_ms, "detail": detail}
        level = log.warning if status in FAILURE_STATUSES or status is UnitStatus.STALE else log.info
        level("%s %s as_of=%s status=%s regime=%s dq=%s persisted=%s %.0fms%s", u.symbol, u.timeframe,
              expected.isoformat(), status.value, event["regime"], event["data_quality"], persisted, dur_ms,
              f" | {detail}" if detail else "")
        try:
            self.runs.record_event(self.health.run_id, event)
        except StorageError as e:
            log.error("could not record monitor event: %s", e)
        # Phase 5: structure is a separate step AFTER the regime result is final for this attempt.
        # It never alters the regime outcome or the stored RegimeSnapshot.
        if self.structure is not None and snap is not None:
            self._process_structure(u, Symbol(u.symbol), tf, expected, now, cyc)
        # Phase 6: setup detection after structure; own status, never alters earlier results.
        if self.setups is not None and snap is not None:
            self._last_regime[u.key] = snap
            self._process_setup(u, Symbol(u.symbol), tf, expected, snap, cyc)

    # ================================================================ setups
    def _setup_pending(self, u: UnitState, as_of: datetime) -> bool:
        return (self.setups is not None and u.setup_attempt_bar == as_of and u.setup_last_as_of != as_of
                and u.setup_status in SETUP_RETRYABLE)

    def _process_setup(self, u: UnitState, sym: Symbol, tf: Timeframe, as_of: datetime,
                       regime: RegimeSnapshot | None, cyc: CycleSummary) -> None:
        h = self.health
        if u.setup_attempt_bar != as_of:
            u.setup_attempt_bar, u.setup_attempts = as_of, 0
        t0 = time.perf_counter()
        persisted, snap = "not_attempted", None
        try:
            snap = self.setups.analyze(sym, tf, as_of, regime)  # type: ignore[union-attr]
        except Exception as e:
            status, detail = SetupStepStatus.ERROR, f"{type(e).__name__}: {e}"
        else:
            try:
                created = self.setups.store.save(snap)  # type: ignore[union-attr]
            except Exception as e:
                status, detail, persisted = SetupStepStatus.PERSISTENCE_ERROR, f"{type(e).__name__}: {e}", "failed"
            else:
                persisted = "saved" if created else "duplicate"
                h.setup_snapshots_created += int(created)
                stats = getattr(self.setups.store, "last", None)
                h.setups_recorded += stats.setups_new if stats is not None else 0
                if snap.data_quality is QualityStatus.VALID:
                    status, detail = SetupStepStatus.OK, ""
                    u.setup_active = len(snap.active)
                else:
                    status, detail = SetupStepStatus.UNAVAILABLE, snap.data_quality.value
        if status in SETUP_RETRYABLE:
            u.setup_attempts += 1
            h.setup_failures += 1
            cyc.errors += 1
            if u.setup_attempts >= self.cfg.unit_max_attempts:
                status = SetupStepStatus.RETRY_EXHAUSTED
                u.setup_last_as_of = as_of
        else:
            u.setup_last_as_of = as_of
        u.setup_status, u.setup_detail = status, detail
        h.count_key(f"setup_{status.value}")
        dur = round((time.perf_counter() - t0) * _MS_PER_S, 1)
        level = log.info if status in (SetupStepStatus.OK, SetupStepStatus.UNAVAILABLE) else log.warning
        level("%s %s as_of=%s setups=%s active=%s persisted=%s %.0fms%s", u.symbol, u.timeframe, as_of.isoformat(),
              status.value, u.setup_active, persisted, dur, f" | {detail}" if detail else "")

    # ============================================================ structure
    def _structure_pending(self, u: UnitState, as_of: datetime, now: datetime) -> bool:
        return (self.structure is not None and u.structure_attempt_bar == as_of
                and u.structure_last_as_of != as_of and u.structure_status in STRUCTURE_RETRYABLE)

    def _process_structure(self, u: UnitState, sym: Symbol, tf: Timeframe, as_of: datetime, now: datetime,
                           cyc: CycleSummary) -> None:
        h = self.health
        if u.structure_attempt_bar != as_of:
            u.structure_attempt_bar, u.structure_attempts = as_of, 0
        t0 = time.perf_counter()
        persisted = "not_attempted"
        snap = None
        try:
            snap = self.structure.analyze(sym, tf, as_of)  # type: ignore[union-attr]
        except Exception as e:
            status, detail = StructureStatus.ERROR, f"{type(e).__name__}: {e}"
        else:
            try:
                created = self.structure.store.save(snap)  # type: ignore[union-attr]
            except Exception as e:
                status, detail, persisted = StructureStatus.PERSISTENCE_ERROR, f"{type(e).__name__}: {e}", "failed"
            else:
                persisted = "saved" if created else "duplicate"
                h.structure_snapshots_created += int(created)
                stats = getattr(self.structure.store, "last", None)     # SaveStats of the SQLite store
                h.structure_events_recorded += stats.events_inserted if stats is not None else 0
                if snap.data_quality is QualityStatus.VALID:
                    status, detail = StructureStatus.OK, ""
                else:
                    status, detail = StructureStatus.UNAVAILABLE, snap.data_quality.value
        if status in STRUCTURE_RETRYABLE:
            u.structure_attempts += 1
            h.structure_failures += 1
            cyc.errors += 1
            if u.structure_attempts >= self.cfg.unit_max_attempts:
                status = StructureStatus.RETRY_EXHAUSTED
                u.structure_last_as_of = as_of
        else:
            u.structure_last_as_of = as_of
        u.structure_status, u.structure_detail = status, detail
        if snap is not None and snap.data_quality is QualityStatus.VALID:
            u.structure_direction = snap.direction.value
        h.count_key(f"structure_{status.value}")
        dur = round((time.perf_counter() - t0) * _MS_PER_S, 1)
        level = log.info if status in (StructureStatus.OK, StructureStatus.UNAVAILABLE) else log.warning
        level("%s %s as_of=%s structure=%s direction=%s persisted=%s %.0fms%s", u.symbol, u.timeframe,
              as_of.isoformat(), status.value, u.structure_direction, persisted, dur, f" | {detail}" if detail else "")

    # ============================================================= process
    def _process(self, u: UnitState, sym: Symbol, tf: Timeframe, as_of: datetime, now: datetime
                 ) -> tuple[UnitStatus, RegimeSnapshot | None, str, str]:
        """Returns (status, snapshot, persisted, detail). Never raises."""
        notes: list[str] = []
        # 1. base series: blocking
        try:
            self._sync_series(sym, tf, as_of)
        except Exception as e:  # classified below, never swallowed into success
            st = classify_data_error(e)
            if st in (UnitStatus.API_TEMPORARY_ERROR, UnitStatus.API_ERROR, UnitStatus.INVALID_DATA):
                self._api_failure(e)
            return st, None, "not_attempted", f"base series sync failed: {type(e).__name__}: {e}"
        # 2. higher timeframes + BTC context: non-blocking (reported by the regime snapshot itself)
        for other_sym, other_tf in self._context_series(sym, tf):
            try:
                self._sync_series(other_sym, other_tf, as_of)
            except Exception as e:
                if classify_data_error(e) in (UnitStatus.API_TEMPORARY_ERROR, UnitStatus.API_ERROR,
                                              UnitStatus.INVALID_DATA):
                    self._api_failure(e)
                notes.append(f"context series {other_sym.name}/{other_tf.value} not synced: {type(e).__name__}")
        # 3. is the expected closed bar stored?
        if not self.market.candle_times(sym, tf, as_of - tf.delta, as_of):
            waited = now - (as_of + self._grace)
            if waited < self._stale_after:
                return UnitStatus.WAITING_FOR_BAR, None, "not_attempted", \
                    f"bar closing {as_of.isoformat()} not delivered yet ({int(waited.total_seconds())}s)"
            return UnitStatus.STALE, None, "not_attempted", \
                f"bar closing {as_of.isoformat()} still missing after {int(waited.total_seconds())}s"
        # 4. Feature Engine
        try:
            fs, btc_fs, tfs = self.regime.compute_features(sym, as_of, tf, self.tfs)
        except Exception as e:
            return UnitStatus.FEATURE_ERROR, None, "not_attempted", f"{type(e).__name__}: {e}"
        # 5. Regime Engine
        try:
            snap = self.regime.classify(sym, tf, fs, btc_fs, tfs)
        except Exception as e:
            return UnitStatus.REGIME_ERROR, None, "not_attempted", f"{type(e).__name__}: {e}"
        self.health.analyses_run += 1
        # 6. outcome
        q = snap.data_quality
        if q is QualityStatus.VALID:
            status = UnitStatus.OK
        elif q in _WARMUP_QUALITIES:
            status = UnitStatus.WARMUP_INSUFFICIENT
            u.bars_available = len(self.market.candle_times(sym, tf, as_of - tf.delta * self.required_bars, as_of))
            notes.append(f"history {u.bars_available}/{self.required_bars} contiguous bars")
        elif q is QualityStatus.STALE:
            status = UnitStatus.STALE
        else:
            status = UnitStatus.DATA_QUALITY_FAILURE
            notes.append(next((c for c in snap.reason_codes if c.startswith("UNKNOWN:")), q.value))
        u.bars_required = self.required_bars
        # 7. persist (idempotent). UNKNOWN snapshots are stored too: they record WHY.
        try:
            created = self.regime.store.save(snap)  # type: ignore[union-attr]
        except Exception as e:
            return UnitStatus.PERSISTENCE_ERROR, snap, "failed", f"{type(e).__name__}: {e}"
        if created:
            self.health.snapshots_created += 1
            self.health.last_snapshot_saved_at = now
        else:
            self.health.snapshots_duplicate += 1
        return status, snap, "saved" if created else "duplicate", "; ".join(notes)

    def _context_series(self, sym: Symbol, base: Timeframe) -> list[tuple[Symbol, Timeframe]]:
        out = [(sym, t) for t in self.tfs if t is not base]
        if sym != self.btc:
            out.append((self.btc, self.btc_tf))
        return out

    def _apply(self, u: UnitState, status: UnitStatus, as_of: datetime, now: datetime,
               snap: RegimeSnapshot | None, detail: str) -> None:
        if u.attempt_bar != as_of:                         # a new bar: fresh attempt budget
            u.attempt_bar, u.attempts_for_bar = as_of, 0
        u.status, u.detail = status, detail
        if snap is not None:
            u.last_regime, u.last_data_quality = snap.regime.value, snap.data_quality.value
        if status is UnitStatus.WAITING_FOR_BAR:
            u.next_attempt_at = None                       # re-check on the next poll, no penalty
            return
        if status in RETRYABLE_STATUSES:
            u.attempts_for_bar += 1
            u.consecutive_failures += 1
            if u.attempts_for_bar >= self.cfg.unit_max_attempts:
                u.status = UnitStatus.RETRY_EXHAUSTED
                u.detail = f"gave up on bar {as_of.isoformat()} after {u.attempts_for_bar} attempts: {detail}"
                self.health.count(UnitStatus.RETRY_EXHAUSTED)
                u.last_processed_as_of, u.attempts_for_bar = as_of, 0
            u.next_attempt_at = now + timedelta(seconds=self.cfg.backoff_s(u.consecutive_failures))
            return
        # terminal outcome for this bar
        u.last_processed_as_of = as_of
        u.attempts_for_bar = 0
        if status in FAILURE_STATUSES:                     # e.g. API_ERROR: next bar, but with backoff
            u.consecutive_failures += 1
            u.next_attempt_at = now + timedelta(seconds=self.cfg.backoff_s(u.consecutive_failures))
        else:
            u.consecutive_failures = 0
            u.next_attempt_at = None
        if status is UnitStatus.OK:
            u.last_ok_as_of = as_of

    # ================================================================= data
    def _sync_instruments(self) -> None:
        names = sorted({s.name for s in self.cfg.symbols} | {self.btc.name})
        try:
            self.backfill.sync_instruments(Category.LINEAR, names)
            self.instruments_synced = True
            self._api_success()
        except LookupError as e:        # configuration error: symbol does not exist -> disable, don't retry
            for n in names:
                if n in str(e):
                    self.disabled_symbols[n] = str(e)
                    for u in self.units:
                        if u.symbol == n:
                            u.status, u.detail = UnitStatus.API_ERROR, f"symbol disabled: {e}"
            log.error("configuration error, symbols disabled for this run: %s", e)
            self.instruments_synced = True
        except Exception as e:           # network etc.: retried on the next tick (instruments only give listing time)
            self._api_failure(e)
            log.warning("instrument sync failed (will retry next cycle): %s: %s", type(e).__name__, e)

    def _sync_series(self, sym: Symbol, tf: Timeframe, as_of: datetime) -> None:
        """Make sure the closed bars of (sym, tf) up to tf.floor(as_of) are stored.
        Local store first; network only for bars that are missing."""
        target = tf.floor(as_of)
        st = self.series.setdefault((sym.name, tf), SeriesState())
        if st.synced_through is not None and st.synced_through >= target:
            return
        window_start = target - tf.delta * self.required_bars
        if not st.warmup_done:
            have = self.market.candle_times(sym, tf, window_start, target)
            if len(have) == self.required_bars:            # restart: history already complete locally
                st.warmup_done, st.synced_through = True, target
                return
            start = window_start                           # one-time warmup backfill
        else:
            start = max(st.synced_through or window_start, window_start)
        if start >= target:
            return
        before = self.backfill.metrics.api_requests
        self.backfill.backfill(sym, tf, start, target)
        st.warmup_done = True
        if self.backfill.metrics.api_requests > before:
            self._api_success()
        last = self.market.last_candles(sym, tf, target, 1)
        st.synced_through = last[0].close_time if last else None

    # ================================================================ health
    def _api_success(self) -> None:
        h = self.health
        h.last_api_success_at = self.clock.now()
        h.consecutive_api_failures = 0
        h.api_status = ApiStatus.OK

    def _api_failure(self, e: BaseException) -> None:
        h = self.health
        h.last_api_error_at = self.clock.now()
        h.last_api_error = f"{type(e).__name__}: {e}"[:_ERROR_TEXT_LIMIT]
        h.consecutive_api_failures += 1
        h.api_status = ApiStatus.DOWN if h.consecutive_api_failures >= self.cfg.api_down_after_failures \
            else ApiStatus.DEGRADED

    def _maybe_heartbeat(self, now: datetime) -> None:
        if self._last_heartbeat is not None and \
                now - self._last_heartbeat < timedelta(seconds=self.cfg.heartbeat_interval_s):
            return
        self._last_heartbeat = now
        h = self.health
        d = h.to_dict()
        log.info("heartbeat: cycles=%d errors=%d snapshots=%d api=%s stale=%s unavailable=%s", h.cycles_total,
                 h.cycles_with_errors, h.snapshots_created, h.api_status.value, d["stale_units"],
                 d["unavailable_units"])
        try:
            self.runs.heartbeat(h.run_id, now, h.status.value, d)
        except StorageError as e:
            log.error("heartbeat not persisted: %s", e)
