"""Shared numerical methodology. Pure float functions over Python lists.

Precision policy: inputs arrive as Decimal (exact exchange values) and are
converted to IEEE-754 double ONCE in ValidatedSeries. Features are statistics,
so float is appropriate; results are checked for finiteness before use.
Money (PnL, fees) stays Decimal in the accounting layer and never passes here.

Warmup criterion (single constant for every recursive filter):
    residual weight of the seed <= e^-6 (~0.25 %)
    => k = ceil(6 / -ln(1 - alpha)) recursive steps after seeding.
    EMA(N): alpha = 2/(N+1)  -> EMA-50: 150, EMA-200: 600 (~3N)
    Wilder(N): alpha = 1/N   -> ATR-14: 81
"""
from __future__ import annotations

import math
from dataclasses import dataclass

SEED_RESIDUAL_LOG = 6.0          # e^-6 ~= 0.248 % residual seed weight


def recursive_warmup(alpha: float) -> int:
    if not 0 < alpha < 1:
        raise ValueError("alpha must be in (0, 1)")
    return math.ceil(SEED_RESIDUAL_LOG / -math.log(1.0 - alpha))


def ema_alpha(n: int) -> float:
    return 2.0 / (n + 1)


def wilder_alpha(n: int) -> float:
    return 1.0 / n


@dataclass(frozen=True)
class NotAvailable:
    """A feature that is mathematically undefined for this input (e.g. zero
    baseline volume). Becomes FeatureStatus.NOT_AVAILABLE with this reason."""
    reason: str


def ema_last(values: list[float], n: int) -> float:
    """Windowed EMA with SMA seed: seed = mean(values[:n]); then
    e_t = alpha * x_t + (1 - alpha) * e_{t-1} over values[n:].
    Deterministic: depends only on `values` (exactly the window passed)."""
    if len(values) < n:
        raise ValueError(f"ema needs >= {n} values, got {len(values)}")
    a = ema_alpha(n)
    e = math.fsum(values[:n]) / n
    for x in values[n:]:
        e = a * x + (1.0 - a) * e
    return e


def true_ranges(h: list[float], l: list[float], c: list[float]) -> list[float]:
    """TR_t = max(H_t - L_t, |H_t - C_{t-1}|, |L_t - C_{t-1}|) for t >= 1."""
    return [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]


def wilder_last(values: list[float], n: int) -> float:
    """Wilder smoothing: seed = mean(values[:n]); s_t = (s_{t-1} * (n-1) + x_t) / n."""
    if len(values) < n:
        raise ValueError(f"wilder needs >= {n} values, got {len(values)}")
    s = math.fsum(values[:n]) / n
    for x in values[n:]:
        s = (s * (n - 1) + x) / n
    return s


def log_returns(c: list[float]) -> list[float]:
    return [math.log(c[i] / c[i - 1]) for i in range(1, len(c))]


def sample_std(x: list[float]) -> float:
    """Sample standard deviation (ddof = 1)."""
    n = len(x)
    if n < 2:
        raise ValueError("sample std needs >= 2 observations")
    m = math.fsum(x) / n
    return math.sqrt(math.fsum((v - m) ** 2 for v in x) / (n - 1))


def bars_per_year(timeframe_ms: int) -> float:
    """Crypto trades 24/7: 365 days of bars."""
    return 365 * 86_400_000 / timeframe_ms
