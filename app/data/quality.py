"""Pure data-quality functions (no I/O): dedup, ordering, gap detection."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

from app.data.ports import RecordIssue
from app.domain.market import Candle, Timeframe


@dataclass(frozen=True)
class Gap:
    start: datetime        # first missing bar open time
    end: datetime          # exclusive: open time of the next present bar (or range end)
    missing_bars: int

    def __str__(self) -> str:
        return f"{self.start.isoformat()} .. {self.end.isoformat()} ({self.missing_bars} bars)"


def dedupe(candles: Iterable[Candle]) -> tuple[list[Candle], int, list[RecordIssue]]:
    """Sort ascending and remove duplicates by (symbol, timeframe, open_time).

    * identical repeats  -> one kept, counted as duplicate
    * DIFFERENT values   -> the key is dropped entirely and reported as
      conflicting_duplicate: we cannot know which one is right, so neither is
      stored. The hole is then visible to gap detection.
    """
    by_key: dict[tuple, Candle] = {}
    conflicted: set[tuple] = set()
    dups = 0
    issues: list[RecordIssue] = []
    for c in candles:
        k = (c.symbol, c.timeframe, c.open_time)
        if k in conflicted:
            continue
        prev = by_key.get(k)
        if prev is None:
            by_key[k] = c
        elif prev.same_values(c) and prev.is_closed == c.is_closed:
            dups += 1
        else:
            conflicted.add(k)
            del by_key[k]
            issues.append(RecordIssue("conflicting_duplicate",
                                      f"{c.symbol} {c.timeframe.value}: two different candles "
                                      "for the same open_time; both rejected", c.open_time))
    return sorted(by_key.values(), key=lambda c: c.open_time), dups, issues


def order_issue(times_ms: Sequence[int], label: str) -> RecordIssue | None:
    """Bybit returns newest-first. Strictly monotonic in either direction is fine;
    anything else is reported (data is still sorted before use)."""
    if len(times_ms) < 2:
        return None
    inc = all(a < b for a, b in zip(times_ms, times_ms[1:]))
    dec = all(a > b for a, b in zip(times_ms, times_ms[1:]))
    if inc or dec:
        return None
    return RecordIssue("out_of_order", f"{label}: response rows are not monotonic in time")


def find_gaps(times: Sequence[datetime], timeframe: Timeframe,
              expected_start: datetime | None = None,
              expected_end: datetime | None = None) -> list[Gap]:
    """times: sorted, unique, aligned bar open times.
    expected_start: first bar that SHOULD exist (e.g. max(requested start, listing time)).
    expected_end: exclusive end of the range that should be fully covered.
    A market that did not trade yet (before listing) is not a gap: callers pass
    the listing time as expected_start."""
    step = timeframe.delta
    gaps: list[Gap] = []
    points = list(times)
    if expected_start is not None:
        expected_start = timeframe.ceil(expected_start)
        if not points or points[0] > expected_start:
            first_present = points[0] if points else (expected_end or expected_start)
            if first_present > expected_start:
                gaps.append(Gap(expected_start, first_present, (first_present - expected_start) // step))
    for a, b in zip(points, points[1:]):
        if b - a > step:
            gaps.append(Gap(a + step, b, (b - a) // step - 1))
        elif b - a < step:
            raise ValueError(f"times not sorted/unique/aligned: {a} -> {b}")
    if expected_end is not None and points:
        nxt = points[-1] + step
        if nxt < expected_end:
            gaps.append(Gap(nxt, expected_end, (expected_end - nxt) // step))
    return gaps


VWAP_TOLERANCE = "0.005"   # shared with research (vt_anomaly feature)


def unit_issue(c: Candle, tolerance: str = VWAP_TOLERANCE) -> RecordIssue | None:
    """Unit sanity: turnover (quote) / volume (base) is the bar VWAP and must lie
    inside [low, high]. A violation means volume/turnover were swapped or mixed
    with another unit (the old CVD/volume confusion). Reported, not auto-fixed."""
    from decimal import Decimal
    if c.volume == 0:
        if c.turnover != 0:
            return RecordIssue("unit_suspect", f"{c.symbol} {c.open_time}: turnover>0 with volume=0", c.open_time)
        return None
    tol = Decimal(tolerance)
    vwap = c.turnover / c.volume
    if not c.low * (1 - tol) <= vwap <= c.high * (1 + tol):
        return RecordIssue("unit_suspect",
                           f"{c.symbol} {c.open_time}: turnover/volume={vwap:.6g} outside [low,high]="
                           f"[{c.low},{c.high}] - volume/turnover units mixed?", c.open_time)
    return None
