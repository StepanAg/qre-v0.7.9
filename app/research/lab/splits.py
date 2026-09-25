"""Chronological train / validation / test splits with purge and embargo.

Boundaries are TIME-based on the dataset range (never shuffled). An item belongs to
the split containing its start (setup anchor / trade decision). Purge: an item whose
window (outcome horizon or trade) ends at/after its split's end is dropped, because its
label would overlap the next split. Embargo: items starting within `embargo_bars`
after a boundary are dropped to damp serial correlation across the boundary."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Generic, Sequence, TypeVar

from app.domain.market import Timeframe

T = TypeVar("T")
NAMES = ("train", "validation", "test")


@dataclass(frozen=True)
class SplitPlan:
    boundaries: tuple[datetime, datetime, datetime, datetime]   # start, b1, b2, end
    embargo: timedelta

    def ranges(self) -> dict[str, tuple[datetime, datetime]]:
        b = self.boundaries
        return {NAMES[i]: (b[i], b[i + 1]) for i in range(3)}

    def to_dict(self) -> dict:
        return {n: [a.isoformat(), z.isoformat()] for n, (a, z) in self.ranges().items()} | \
               {"embargo_s": self.embargo.total_seconds()}


def plan(start: datetime, end: datetime, fractions: Sequence[float], tf: Timeframe, embargo_bars: int) -> SplitPlan:
    total = (end - start) // tf.delta
    c1 = round(total * fractions[0])
    c2 = round(total * (fractions[0] + fractions[1]))
    return SplitPlan((start, start + tf.delta * c1, start + tf.delta * c2, end), tf.delta * embargo_bars)


@dataclass
class Assignment(Generic[T]):
    split: dict[str, list[T]]
    purged: int
    embargoed: int
    outside: int


def assign(items: Sequence[T], p: SplitPlan, start_of: Callable[[T], datetime],
           end_of: Callable[[T], datetime]) -> Assignment[T]:
    out: dict[str, list[T]] = {n: [] for n in NAMES}
    purged = embargoed = outside = 0
    for it in items:
        s = start_of(it)
        name = next((n for n, (a, z) in p.ranges().items() if a <= s < z), None)
        if name is None:
            outside += 1
            continue
        a, z = p.ranges()[name]
        if name != "train" and s < a + p.embargo:
            embargoed += 1
            continue
        if end_of(it) >= z and name != "test":
            purged += 1
            continue
        out[name].append(it)
    return Assignment(out, purged, embargoed, outside)
