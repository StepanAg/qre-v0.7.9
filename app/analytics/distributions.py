"""Distributions with the sample-size rules approved in decision D2."""
from __future__ import annotations

from typing import Sequence

from app.research.lab import stats

MIN_ROBUST_N = 30       # below: a warning accompanies every statistic
MIN_QUARTILE_N = 4      # p25 / p75 only from n >= 4
MIN_DECILE_N = 20       # p10 / p90 only from n >= 20


def warning(n: int) -> str | None:
    return stats.sample_warning(n, MIN_ROBUST_N)


def distribution(xs: Sequence[float], what: str) -> dict:
    xs = [float(x) for x in xs]
    d = {"n": len(xs),
         "mean": stats.mean(xs, what), "median": stats.median(xs, what),
         "p25": stats.percentile(xs, 25, what, MIN_QUARTILE_N), "p75": stats.percentile(xs, 75, what, MIN_QUARTILE_N),
         "p10": stats.percentile(xs, 10, what, MIN_DECILE_N), "p90": stats.percentile(xs, 90, what, MIN_DECILE_N),
         "min": stats.value(min(xs)) if xs else stats.na(f"no_valid_{what}"),
         "max": stats.value(max(xs)) if xs else stats.na(f"no_valid_{what}")}
    w = warning(len(xs))
    if w:
        d["warning"] = w
    return d
