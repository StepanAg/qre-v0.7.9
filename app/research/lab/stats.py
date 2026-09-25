"""Metric helpers: a metric is either a value or an explicit 'not_available' with a reason."""
from __future__ import annotations

import statistics
from typing import Sequence


def value(v) -> dict:
    return {"value": v}


def na(reason: str) -> dict:
    return {"status": "not_available", "reason": reason}


def mean(xs: Sequence[float], what: str) -> dict:
    return value(sum(xs) / len(xs)) if xs else na(f"no_valid_{what}")


def median(xs: Sequence[float], what: str) -> dict:
    return value(statistics.median(xs)) if xs else na(f"no_valid_{what}")


def ratio(num: float, den: float, what: str) -> dict:
    return value(num / den) if den else na(f"zero_denominator:{what}")


# ---- Phase 8 additions (new functions only; the functions above are unchanged) ----
def percentile(xs: Sequence[float], p: float, what: str, min_n: int = 1) -> dict:
    """Linear interpolation between closest ranks: rank = p/100 * (n - 1), p in [0, 100]."""
    if not 0 <= p <= 100:
        raise ValueError(f"percentile out of range: {p}")
    if not xs:
        return na(f"no_valid_{what}")
    if len(xs) < min_n:
        return na(f"sample_too_small_for:p{p:g}(n={len(xs)}<{min_n})")
    s = sorted(xs)
    k = (len(s) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return value(s[lo] + (s[hi] - s[lo]) * (k - lo))


def sample_warning(n: int, min_robust: int) -> str | None:
    if n < min_robust:
        return f"Sample size = {n}. Statistics are not statistically robust."
    return None
