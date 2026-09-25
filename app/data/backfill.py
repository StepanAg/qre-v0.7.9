"""Backfill orchestration:
requested range -> provider (pagination, dedup, sort, parse) -> drop open candle
-> quality checks (gaps) -> persist (idempotent) -> run record.

Errors propagate. A failed run is recorded as 'failed' and the exception is
re-raised; nothing is converted into an empty dataset."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

from app.core.clock import Clock, SystemClock
from app.core.ids import new_id
from app.core.logging import get_logger
from app.data.errors import InsufficientHistory
from app.data.metrics import IngestionMetrics
from app.data.ports import (BackfillRunRecord, MarketDataProvider, MarketDataSink, MarketDataStore,
                            RecordIssue)
from app.data.quality import Gap, find_gaps
from app.domain.enums import Category
from app.domain.market import Candle, Symbol, Timeframe

log = get_logger("data.backfill")



@dataclass(frozen=True)
class HistoryPlan:
    timeframe: Timeframe
    usable_bars: int       # decision points the consumer needs values for
    warmup_bars: int       # extra bars needed before the first usable point
    total_bars: int        # usable + warmup
    bars_per_value: int    # contiguous closed bars one value depends on
    start: datetime
    end: datetime          # exclusive, aligned

    def describe(self) -> str:
        return (f"{self.timeframe.value}: requested(usable)={self.usable_bars} "
                f"warmup={self.warmup_bars} total={self.total_bars} (bars/value={self.bars_per_value}) "
                f"range=[{self.start.isoformat()}, {self.end.isoformat()})")


def plan_history(timeframe: Timeframe, usable_bars: int, bars_per_value: int, end: datetime) -> HistoryPlan:
    """History depends on the task. `bars_per_value` is NOT decided here: it comes
    from the feature requirements (FeatureSpec.min_observations, e.g. EMA-200 = 800
    = 200 seed + 600 recursive warmup). The data layer holds no indicator
    methodology, so it cannot drift from the engine.

    total = usable + bars_per_value - 1  (each usable point needs its own window)."""
    if usable_bars < 1 or bars_per_value < 1:
        raise ValueError("usable_bars >= 1 and bars_per_value >= 1 required")
    end = timeframe.floor(end)
    warmup = bars_per_value - 1
    total = usable_bars + warmup
    return HistoryPlan(timeframe, usable_bars, warmup, total, bars_per_value,
                       end - timeframe.delta * total, end)


@dataclass(frozen=True)
class BackfillReport:
    run_id: str
    symbol: Symbol
    timeframe: Timeframe
    start: datetime
    end: datetime
    status: str            # ok | ok_with_gaps | completed_with_issues
    pages: int
    fetched: int
    inserted: int
    unchanged: int
    duplicates_in_response: int
    conflicts: int
    invalid: int
    open_excluded: int
    gaps: tuple[Gap, ...]
    issues: tuple[RecordIssue, ...]
    duration_s: float
    db_write_s: float


class BackfillService:
    def __init__(self, provider: MarketDataProvider, store: MarketDataStore, sink: MarketDataSink,
                 *, clock: Clock | None = None, metrics: IngestionMetrics | None = None) -> None:
        self.provider = provider
        self.store = store
        self.sink = sink
        self.clock = clock or SystemClock()
        self.metrics = metrics or IngestionMetrics()

    # ---------------------------------------------------------- instruments
    def sync_instruments(self, category: Category, symbols: list[str] | None = None) -> int:
        items = self.provider.instruments(category, symbols)
        if symbols:
            missing = set(symbols) - {i.symbol.name for i in items}
            if missing:
                raise LookupError(f"instruments not found on exchange: {sorted(missing)}")
        n = self.sink.upsert_instruments(items, self.provider.name)
        self.metrics.symbols_loaded += n
        return n

    # -------------------------------------------------------------- candles
    def backfill(self, symbol: Symbol, timeframe: Timeframe, start: datetime, end: datetime) -> BackfillReport:
        t0 = time.perf_counter()
        now_floor = timeframe.floor(self.clock.now())
        start, end = timeframe.ceil(start), min(timeframe.ceil(end), now_floor)
        if end <= start:
            raise ValueError(f"empty backfill range after clamping to closed bars: [{start}, {end})")
        run = BackfillRunRecord(new_id("evt"), "candles", symbol, timeframe, start, end, self.clock.now())
        try:
            batch = self.provider.candles(symbol, timeframe, start, end)
            closed = [c for c in batch.candles if c.is_closed]
            open_excluded = len(batch.candles) - len(closed)

            w0 = time.perf_counter()
            res = self.sink.upsert_candles(closed, self.provider.name)
            db_s = time.perf_counter() - w0

            available_from = self._available_from(symbol, start)
            stored = self.store.candle_times(symbol, timeframe, start, end)
            gaps = find_gaps(stored, timeframe, available_from, end)
            issues = list(batch.issues) + [RecordIssue(c.kind, c.detail, c.at, c.raw) for c in res.conflicts]
            issues += [RecordIssue("gap", str(g), g.start) for g in gaps]
            invalid = sum(1 for i in batch.issues if i.kind in ("invalid_record", "conflicting_duplicate"))
            serious = invalid or res.conflicts
            status = "completed_with_issues" if serious else ("ok_with_gaps" if gaps else "ok")

            run.finished_at, run.status, run.pages = self.clock.now(), status, batch.pages
            run.fetched, run.inserted, run.unchanged = len(batch.candles), res.inserted, res.unchanged
            run.conflicts, run.invalid, run.open_excluded = len(res.conflicts), invalid, open_excluded
            run.gaps, run.issues = len(gaps), issues
            self.sink.record_run(run)

            m = self.metrics
            m.candles_inserted += res.inserted
            m.duplicates_rejected += res.unchanged + batch.duplicates_in_response
            m.conflicts += len(res.conflicts)
            m.open_candles_excluded += open_excluded
            m.gaps_detected += len(gaps)
            m.db_write_duration_s += db_s
            dur = time.perf_counter() - t0
            m.backfill_duration_s += dur
            if serious or gaps:
                log.warning("backfill %s %s %s: %d gaps, %d invalid, %d conflicts",
                            symbol, timeframe.value, status, len(gaps), invalid, len(res.conflicts))
            return BackfillReport(run.run_id, symbol, timeframe, start, end, status, batch.pages,
                                  len(batch.candles), res.inserted, res.unchanged,
                                  batch.duplicates_in_response, len(res.conflicts), invalid, open_excluded,
                                  tuple(gaps), tuple(issues), dur, db_s)
        except Exception as e:
            run.finished_at, run.status, run.error = self.clock.now(), "failed", f"{type(e).__name__}: {e}"
            try:
                self.sink.record_run(run)
            except Exception as rec_err:  # never mask the original error
                log.error("could not record failed run %s: %s", run.run_id, rec_err)
            log.error("backfill %s %s failed: %s", symbol, timeframe.value, run.error)
            raise

    def _available_from(self, symbol: Symbol, start: datetime) -> datetime:
        info = self.store.instrument(symbol)
        if info is not None and info.launch_time is not None and info.launch_time > start:
            return info.launch_time
        return start

    # ------------------------------------------------------------- history
    def ensure_history(self, symbol: Symbol, timeframe: Timeframe, usable_bars: int, bars_per_value: int,
                       end: datetime | None = None) -> tuple[HistoryPlan, BackfillReport, list[Candle]]:
        """Make sure usable+warmup closed bars exist, then return exactly them.
        Raises InsufficientHistory instead of computing indicators on too little data.
        bars_per_value: take it from the feature requirements (FeatureService.bars_per_value)."""
        plan = plan_history(timeframe, usable_bars, bars_per_value, end or self.clock.now())
        report = self.backfill(symbol, timeframe, plan.start, plan.end)
        candles = self.store.get_candles(symbol, timeframe, plan.start, plan.end)
        if len(candles) < plan.total_bars:
            raise InsufficientHistory(
                f"{symbol} {timeframe.value}: have {len(candles)} bars, need {plan.total_bars} "
                f"({plan.describe()}); gaps={len(report.gaps)}")
        return plan, report, candles

    # ------------------------------------------------------------- funding
    def backfill_funding(self, symbol: Symbol, start: datetime, end: datetime) -> BackfillRunRecord:
        run = BackfillRunRecord(new_id("evt"), "funding", symbol, None, start, end, self.clock.now())
        try:
            batch = self.provider.funding_history(symbol, start, end)
            res = self.sink.upsert_funding(batch.rates, self.provider.name)
            run.issues = list(batch.issues) + [RecordIssue(c.kind, c.detail, c.at, c.raw) for c in res.conflicts]
            run.pages, run.fetched, run.inserted, run.unchanged = batch.pages, len(batch.rates), res.inserted, res.unchanged
            run.conflicts = len(res.conflicts)
            run.invalid = sum(1 for i in batch.issues if i.kind == "invalid_record")
            run.status = "completed_with_issues" if (run.invalid or run.conflicts) else "ok"
            run.finished_at = self.clock.now()
            self.sink.record_run(run)
            self.metrics.funding_inserted += res.inserted
            return run
        except Exception as e:
            run.finished_at, run.status, run.error = self.clock.now(), "failed", f"{type(e).__name__}: {e}"
            try:
                self.sink.record_run(run)
            except Exception as rec_err:
                log.error("could not record failed run %s: %s", run.run_id, rec_err)
            raise
