"""DataQualityPolicy: the ONE place that decides whether a candle series may be
used by a given feature at a given decision time. Indicators never re-implement
gap/stale/open-candle rules.

Rules (docs/V070_DATA_QUALITY_POLICY.md):
* Point-in-time: only bars with close_time <= as_of exist for the policy.
* Open candles (is_closed=False) are excluded and counted, never used.
* Duplicates / foreign symbol or timeframe  -> series UNUSABLE.
* The bar that should have closed at as_of is missing -> STALE.
* Gaps are NEVER filled. A feature is computed only on the most recent
  CONTIGUOUS run of bars; it needs `min_observations` bars in that run.
  Gap severity is judged relative to that window.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

from app.data.quality import Gap, find_gaps
from app.domain.enums import FeatureStatus, GapSeverity, QualityStatus
from app.domain.market import Candle, Symbol, Timeframe


@dataclass(frozen=True)
class SeriesIssue:
    status: QualityStatus
    detail: str
    at: datetime | None = None


@dataclass
class SeriesStats:
    valid_observations: int = 0
    missing_observations: int = 0     # bars inside gaps
    gaps: int = 0
    invalid_observations: int = 0
    duplicate_observations: int = 0
    open_candle_exclusions: int = 0
    future_exclusions: int = 0        # closed bars with close_time > as_of (not yet known at as_of)


class ValidatedSeries:
    """Closed, sorted, unique, aligned bars known at `as_of`, as float arrays.
    Built only by DataQualityPolicy.prepare(). Decimal -> float happens here, once."""

    def __init__(self, symbol: Symbol, timeframe: Timeframe, as_of: datetime, candles: list[Candle],
                 issues: list[SeriesIssue], stats: SeriesStats, unusable: QualityStatus | None) -> None:
        self.symbol, self.timeframe, self.as_of = symbol, timeframe, as_of
        self.candles = candles
        self.times = [c.open_time for c in candles]
        self.o = [float(c.open) for c in candles]
        self.h = [float(c.high) for c in candles]
        self.l = [float(c.low) for c in candles]
        self.c = [float(c.close) for c in candles]
        self.v = [float(c.volume) for c in candles]
        self.q = [float(c.turnover) for c in candles]
        self.issues = issues
        self.stats = stats
        self.unusable = unusable
        step = timeframe.delta
        # index i is a "break" when bar i does not directly follow bar i-1
        self.breaks = [i for i in range(1, len(self.times)) if self.times[i] - self.times[i - 1] != step]
        self.expected_last_open = timeframe.floor(as_of) - step
        self.stale = bool(self.times) and self.times[-1] < self.expected_last_open
        if not self.times:
            self.stale = False

    def __len__(self) -> int:
        return len(self.times)

    def tail_contiguous(self) -> int:
        """Number of bars in the most recent contiguous run."""
        if not self.times:
            return 0
        return len(self.times) - (self.breaks[-1] if self.breaks else 0)

    def last_break_time(self) -> datetime | None:
        return self.times[self.breaks[-1]] if self.breaks else None

    def gaps(self) -> list[Gap]:
        return find_gaps(self.times, self.timeframe)

    def tail(self, n: int) -> "ValidatedSeries":
        """View of the last n bars with the same as_of (Phase 6, additive): lets a caller
        reproduce exactly the series a service loading only n bars would have seen."""
        if n >= len(self.times):
            return self
        off = len(self.times) - n
        view = ValidatedSeries.__new__(ValidatedSeries)
        view.symbol, view.timeframe, view.as_of = self.symbol, self.timeframe, self.as_of
        view.candles = self.candles[off:]
        view.times, view.o, view.h, view.l = self.times[off:], self.o[off:], self.h[off:], self.l[off:]
        view.c, view.v, view.q = self.c[off:], self.v[off:], self.q[off:]
        stats = SeriesStats(valid_observations=n)
        view.issues, view.stats, view.unusable = [], stats, self.unusable
        view.breaks = [b - off for b in self.breaks if b - off > 0]
        view.expected_last_open = self.expected_last_open
        view.stale = bool(view.times) and view.times[-1] < view.expected_last_open
        stats.gaps = len(view.breaks)
        return view

    def upto(self, as_of: datetime) -> "ValidatedSeries":
        """Point-in-time view for an earlier decision time (used for coverage /
        backtests). Equivalent to prepare(candles, tf, as_of) on clean data."""
        idx = self._bisect(as_of)
        stats = SeriesStats(valid_observations=idx)
        view = ValidatedSeries.__new__(ValidatedSeries)
        view.symbol, view.timeframe, view.as_of = self.symbol, self.timeframe, as_of
        view.candles = self.candles[:idx]
        view.times, view.o, view.h, view.l = self.times[:idx], self.o[:idx], self.h[:idx], self.l[:idx]
        view.c, view.v, view.q = self.c[:idx], self.v[:idx], self.q[:idx]
        view.issues, view.stats, view.unusable = [], stats, self.unusable
        view.breaks = [b for b in self.breaks if b < idx]
        view.expected_last_open = self.timeframe.floor(as_of) - self.timeframe.delta
        view.stale = bool(view.times) and view.times[-1] < view.expected_last_open
        stats.gaps = len(view.breaks)
        return view

    def _bisect(self, as_of: datetime) -> int:
        # last bar with open_time + tf <= as_of  <=>  open_time <= as_of - tf
        return bisect.bisect_right(self.times, as_of - self.timeframe.delta)


@dataclass(frozen=True)
class Assessment:
    """Policy verdict for one feature on one series."""
    status: FeatureStatus
    quality: QualityStatus
    severity: GapSeverity
    tail_bars: int
    required: int
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is FeatureStatus.VALUE


@dataclass(frozen=True)
class DataQualityPolicy:
    """Stateless policy object; all thresholds live in feature requirements."""

    def prepare(self, candles: Sequence[Candle], timeframe: Timeframe, as_of: datetime,
                symbol: Symbol | None = None) -> ValidatedSeries:
        stats = SeriesStats()
        issues: list[SeriesIssue] = []
        unusable: QualityStatus | None = None
        symbol = symbol or (candles[0].symbol if candles else None)
        known: list[Candle] = []
        for c in candles:
            if c.symbol != symbol or c.timeframe is not timeframe:
                stats.invalid_observations += 1
                unusable = QualityStatus.INVALID
                issues.append(SeriesIssue(QualityStatus.INVALID,
                                          f"bar {c.symbol} {c.timeframe.value} in {symbol} {timeframe.value} series",
                                          c.open_time))
                continue
            if not c.is_closed:
                stats.open_candle_exclusions += 1
                issues.append(SeriesIssue(QualityStatus.OPEN_CANDLE, "unclosed bar excluded", c.open_time))
                continue
            if c.close_time > as_of:
                stats.future_exclusions += 1   # not an error: simply not known yet at as_of
                continue
            known.append(c)
        known.sort(key=lambda c: c.open_time)
        for a, b in zip(known, known[1:]):
            if a.open_time == b.open_time:
                stats.duplicate_observations += 1
                unusable = unusable or QualityStatus.DUPLICATE
                issues.append(SeriesIssue(QualityStatus.DUPLICATE, "two bars with the same open time",
                                          b.open_time))
        if unusable is QualityStatus.DUPLICATE:
            # keep series well-formed for reporting; it is unusable anyway
            dedup: dict[datetime, Candle] = {}
            for c in known:
                dedup.setdefault(c.open_time, c)
            known = [dedup[k] for k in sorted(dedup)]
        s = ValidatedSeries(symbol, timeframe, as_of, known, issues, stats, unusable)  # type: ignore[arg-type]
        stats.valid_observations = len(known)
        gaps = s.gaps()
        stats.gaps = len(gaps)
        stats.missing_observations = sum(g.missing_bars for g in gaps)
        for g in gaps:
            issues.append(SeriesIssue(QualityStatus.GAP, str(g), g.start))
        if s.stale:
            issues.append(SeriesIssue(QualityStatus.STALE,
                                      f"expected bar {s.expected_last_open.isoformat()} missing", as_of))
        return s

    def assess(self, s: ValidatedSeries, min_observations: int, max_gap_bars: int = 0) -> Assessment:
        """Order matters: unusable -> missing -> stale -> contiguity/length."""
        if s.unusable is not None:
            return Assessment(FeatureStatus.DATA_QUALITY_FAILURE, s.unusable, GapSeverity.UNUSABLE, 0,
                              min_observations, f"series unusable: {s.unusable.value}")
        if not s.times:
            return Assessment(FeatureStatus.INSUFFICIENT_HISTORY, QualityStatus.MISSING_HISTORY,
                              GapSeverity.NO_GAP, 0, min_observations, "no closed bars known at as_of")
        if s.stale:
            return Assessment(FeatureStatus.DATA_QUALITY_FAILURE, QualityStatus.STALE, GapSeverity.UNUSABLE,
                              0, min_observations,
                              f"latest closed bar {s.expected_last_open.isoformat()} is missing (stale data)")
        tail = self._tail(s, max_gap_bars)
        has_gaps = bool(s.breaks)
        if tail >= min_observations:
            return Assessment(FeatureStatus.VALUE, QualityStatus.VALID,
                              GapSeverity.MINOR_GAP if has_gaps else GapSeverity.NO_GAP, tail, min_observations)
        if has_gaps and len(s) >= min_observations:
            brk = s.last_break_time()
            return Assessment(FeatureStatus.DATA_QUALITY_FAILURE, QualityStatus.GAP, GapSeverity.MAJOR_GAP,
                              tail, min_observations,
                              f"gap before {brk.isoformat() if brk else '?'} lies inside the required window; "
                              f"{tail}/{min_observations} contiguous bars, recovers after "
                              f"{min_observations - tail} more closed bars")
        return Assessment(FeatureStatus.INSUFFICIENT_HISTORY, QualityStatus.INSUFFICIENT_HISTORY,
                          GapSeverity.MINOR_GAP if has_gaps else GapSeverity.NO_GAP, tail, min_observations,
                          f"{tail}/{min_observations} contiguous closed bars")

    @staticmethod
    def _tail(s: ValidatedSeries, max_gap_bars: int) -> int:
        if max_gap_bars != 0:
            raise ValueError("gap tolerance is not supported: it would glue the series across gaps")
        return s.tail_contiguous()
