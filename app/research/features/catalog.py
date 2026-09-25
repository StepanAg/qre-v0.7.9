"""Catalog of implemented features (v1). Every formula is a module-level
function f(window, **params) so its source is hashable (formula_hash) and it
can be unit-tested directly. Documentation: docs/V070_FEATURE_CATALOG.md."""
from __future__ import annotations

import math

from app.data.quality import VWAP_TOLERANCE
from app.research.features.methodology import (NotAvailable, bars_per_year, ema_alpha, ema_last,
                                               log_returns, recursive_warmup, sample_std, true_ranges,
                                               wilder_alpha, wilder_last)
from app.research.features.spec import FeatureRegistry, FeatureSpec, Window

_VWAP_TOL = float(VWAP_TOLERANCE)


def ema_bars(n: int) -> int:
    """Bars for one windowed EMA value: n (SMA seed) + recursive warmup."""
    return n + recursive_warmup(ema_alpha(n))


def atr_bars(n: int) -> int:
    """1 extra bar for the first previous close + n seed TRs + Wilder warmup."""
    return 1 + n + recursive_warmup(wilder_alpha(n))


# ------------------------------------------------------------------ returns
def f_ret_cc(w: Window) -> float:
    return w.c[-1] / w.c[-2] - 1.0


def f_log_ret(w: Window) -> float:
    return math.log(w.c[-1] / w.c[-2])


def f_ret_oc(w: Window) -> float:
    return w.c[-1] / w.o[-1] - 1.0


def f_hl_range(w: Window) -> float:
    return w.h[-1] - w.l[-1]


def f_hl_range_pct(w: Window) -> float:
    return (w.h[-1] - w.l[-1]) / w.c[-1]


def f_true_range(w: Window) -> float:
    return true_ranges(w.h[-2:], w.l[-2:], w.c[-2:])[-1]


def f_ret_n(w: Window, n: int) -> float:
    return w.c[-1] / w.c[-1 - n] - 1.0


# -------------------------------------------------------------------- trend
def f_sma(w: Window, n: int) -> float:
    return math.fsum(w.c[-n:]) / n


def f_ema(w: Window, n: int) -> float:
    return ema_last(w.c[-ema_bars(n):], n)


def f_dist_ema(w: Window, n: int) -> float:
    return w.c[-1] / ema_last(w.c[-ema_bars(n):], n) - 1.0


def f_ema_slope(w: Window, n: int, k: int) -> float:
    """EMA_t / EMA_{t-k} - 1, each EMA on its own exact window (same as ema_n)."""
    m = ema_bars(n)
    now = ema_last(w.c[-m:], n)
    before = ema_last(w.c[-m - k:-k], n)
    return now / before - 1.0


def f_ema_spread(w: Window, fast: int, slow: int) -> float:
    return ema_last(w.c[-ema_bars(fast):], fast) / ema_last(w.c[-ema_bars(slow):], slow) - 1.0


# --------------------------------------------------------------- volatility
def f_atr(w: Window, n: int) -> float:
    return wilder_last(true_ranges(w.h, w.l, w.c), n)


def f_atr_pct(w: Window, n: int) -> float:
    return wilder_last(true_ranges(w.h, w.l, w.c), n) / w.c[-1]


def f_ret_std(w: Window, n: int) -> float:
    return sample_std(log_returns(w.c[-n - 1:]))


def f_realized_vol(w: Window, n: int) -> float:
    return math.sqrt(math.fsum(r * r for r in log_returns(w.c[-n - 1:])))


def f_vol_annualized(w: Window, n: int) -> float:
    return sample_std(log_returns(w.c[-n - 1:])) * math.sqrt(bars_per_year(w.timeframe.ms))


def f_vol_ratio(w: Window, short: int, long: int) -> float | NotAvailable:
    lo = sample_std(log_returns(w.c[-long - 1:]))
    if lo == 0.0:
        return NotAvailable("zero long-horizon volatility (flat prices)")
    return sample_std(log_returns(w.c[-short - 1:])) / lo


# ------------------------------------------------------------------- volume
def f_volume(w: Window) -> float:
    return w.v[-1]


def f_turnover(w: Window) -> float:
    return w.q[-1]


def f_volume_sum(w: Window, n: int) -> float:
    return math.fsum(w.v[-n:])


def f_turnover_sma(w: Window, n: int) -> float:
    return math.fsum(w.q[-n:]) / n


def f_rel_volume(w: Window, n: int) -> float | NotAvailable:
    """Current bar volume / mean volume of the PREVIOUS n bars."""
    base = math.fsum(w.v[-n - 1:-1]) / n
    if base == 0.0:
        return NotAvailable("zero baseline volume (no trading in previous bars)")
    return w.v[-1] / base


def f_vt_anomaly_count(w: Window, n: int) -> float:
    """Bars whose implied VWAP (turnover/volume) lies outside [low, high]
    (+-tolerance), or turnover > 0 with zero volume: unit/data anomalies."""
    bad = 0
    for h, l, v, q in zip(w.h[-n:], w.l[-n:], w.v[-n:], w.q[-n:]):
        if v == 0.0:
            bad += q != 0.0
        elif not l * (1 - _VWAP_TOL) <= q / v <= h * (1 + _VWAP_TOL):
            bad += 1
    return float(bad)


def f_clv_volume_pressure(w: Window, n: int) -> float | NotAvailable:
    """PROXY (not CVD): sum(V_i * CLV_i) / sum(V_i), CLV = ((C-L)-(H-C))/(H-L), 0 if H == L.
    Assumes volume is distributed according to where the bar closed in its range."""
    num = den = 0.0
    for h, l, c, v in zip(w.h[-n:], w.l[-n:], w.c[-n:], w.v[-n:]):
        clv = ((c - l) - (h - c)) / (h - l) if h > l else 0.0
        num += v * clv
        den += v
    if den == 0.0:
        return NotAvailable("zero volume in window")
    return num / den


# ---------------------------------------------------------------- structure
def f_rolling_high(w: Window, n: int) -> float:
    return max(w.h[-n - 1:-1])


def f_rolling_low(w: Window, n: int) -> float:
    return min(w.l[-n - 1:-1])


def f_dist_high(w: Window, n: int) -> float:
    return w.c[-1] / max(w.h[-n - 1:-1]) - 1.0


def f_dist_low(w: Window, n: int) -> float:
    return w.c[-1] / min(w.l[-n - 1:-1]) - 1.0


def f_range_position(w: Window, n: int) -> float | NotAvailable:
    hi, lo = max(w.h[-n - 1:-1]), min(w.l[-n - 1:-1])
    if hi == lo:
        return NotAvailable("previous range has zero width")
    return (w.c[-1] - lo) / (hi - lo)


def f_volume_concentration(w: Window, n: int) -> float | NotAvailable:
    tot = math.fsum(w.v[-n:])
    if tot == 0.0:
        return NotAvailable("zero volume in window")
    return max(w.v[-n:]) / tot


def f_amihud(w: Window, n: int) -> float | NotAvailable:
    """mean(|log return_i| / turnover_i) * 1e6 over n bars (price impact per 1M quote)."""
    rets = log_returns(w.c[-n - 1:])
    qs = w.q[-n:]
    if any(q == 0.0 for q in qs):
        return NotAvailable("bar with zero turnover in window")
    return math.fsum(abs(r) / q for r, q in zip(rets, qs)) / n * 1e6


# ------------------------------------------------------------------ catalog
def _s(name, family, unit, fn, lookback, min_obs, desc, *, warmup=0, params=None, interp="", limits="",
       **kw) -> FeatureSpec:
    return FeatureSpec(name=name, version=1, family=family, unit=unit, description=desc, fn=fn,
                       lookback=lookback, min_observations=min_obs, warmup=warmup, params=params or {},
                       interpretation=interp, limitations=limits, **kw)


def _ema_warm(n: int) -> int:
    return recursive_warmup(ema_alpha(n))


def build_default_registry() -> FeatureRegistry:
    atr_warm = recursive_warmup(wilder_alpha(14))
    specs = [
        # returns
        _s("ret_cc", "returns", "fraction", f_ret_cc, 1, 2, "close-to-close simple return C_t/C_{t-1}-1"),
        _s("log_ret", "returns", "log", f_log_ret, 1, 2, "log return ln(C_t/C_{t-1})"),
        _s("ret_oc", "returns", "fraction", f_ret_oc, 1, 1, "open-to-close return C_t/O_t-1"),
        _s("hl_range", "returns", "quote", f_hl_range, 1, 1, "bar range H_t-L_t (price units)"),
        _s("hl_range_pct", "returns", "fraction", f_hl_range_pct, 1, 1, "normalized range (H_t-L_t)/C_t"),
        _s("true_range", "returns", "quote", f_true_range, 1, 2, "max(H-L, |H-C_prev|, |L-C_prev|)"),
        _s("ret_20", "returns", "fraction", f_ret_n, 20, 21, "rolling return C_t/C_{t-20}-1", params={"n": 20}),
        # trend
        _s("sma_20", "trend", "quote", f_sma, 20, 20, "simple moving average of close, 20 bars", params={"n": 20}),
        _s("sma_50", "trend", "quote", f_sma, 50, 50, "simple moving average of close, 50 bars", params={"n": 50}),
        _s("ema_20", "trend", "quote", f_ema, 20, ema_bars(20), "windowed EMA(20), SMA seed",
           warmup=_ema_warm(20), params={"n": 20}),
        _s("ema_50", "trend", "quote", f_ema, 50, ema_bars(50), "windowed EMA(50), SMA seed",
           warmup=_ema_warm(50), params={"n": 50}),
        _s("ema_200", "trend", "quote", f_ema, 200, ema_bars(200), "windowed EMA(200), SMA seed",
           warmup=_ema_warm(200), params={"n": 200}),
        _s("dist_ema_50_pct", "trend", "fraction", f_dist_ema, 50, ema_bars(50), "C_t/EMA50_t-1",
           warmup=_ema_warm(50), params={"n": 50}),
        _s("dist_ema_200_pct", "trend", "fraction", f_dist_ema, 200, ema_bars(200), "C_t/EMA200_t-1",
           warmup=_ema_warm(200), params={"n": 200}),
        _s("ema_50_slope_10", "trend", "fraction", f_ema_slope, 50, ema_bars(50) + 10,
           "EMA50_t/EMA50_{t-10}-1", warmup=_ema_warm(50), params={"n": 50, "k": 10}),
        _s("ema_200_slope_10", "trend", "fraction", f_ema_slope, 200, ema_bars(200) + 10,
           "EMA200_t/EMA200_{t-10}-1", warmup=_ema_warm(200), params={"n": 200, "k": 10}),
        _s("ema_50_200_spread", "trend", "fraction", f_ema_spread, 200, ema_bars(200),
           "EMA50_t/EMA200_t-1 (trend relation; compare across timeframes via MTF snapshot)",
           warmup=_ema_warm(200), params={"fast": 50, "slow": 200}),
        # volatility
        _s("atr_14", "volatility", "quote", f_atr, 14, atr_bars(14), "Wilder ATR(14) of true range",
           warmup=atr_warm, params={"n": 14}),
        _s("atr_14_pct", "volatility", "fraction", f_atr_pct, 14, atr_bars(14), "ATR14_t / C_t",
           warmup=atr_warm, params={"n": 14}),
        _s("ret_std_20", "volatility", "log", f_ret_std, 20, 21,
           "sample std (ddof=1) of 20 log returns, per bar", params={"n": 20}),
        _s("realized_vol_20", "volatility", "log", f_realized_vol, 20, 21,
           "sqrt(sum r^2) over 20 log returns (no mean removal, not annualized)", params={"n": 20}),
        _s("vol_20_annualized", "volatility", "log", f_vol_annualized, 20, 21,
           "ret_std_20 * sqrt(bars per 365-day year)", params={"n": 20}),
        _s("vol_ratio_20_100", "volatility", "ratio", f_vol_ratio, 100, 101,
           "ret_std_20 / ret_std_100 (normalized volatility)", params={"short": 20, "long": 100}),
        # volume
        _s("volume", "volume", "base", f_volume, 1, 1, "bar volume in BASE coin"),
        _s("turnover", "volume", "quote", f_turnover, 1, 1, "bar turnover in QUOTE currency"),
        _s("volume_sum_20", "volume", "base", f_volume_sum, 20, 20, "sum of volume, 20 bars", params={"n": 20}),
        _s("turnover_sma_20", "volume", "quote", f_turnover_sma, 20, 20, "mean turnover, 20 bars",
           params={"n": 20}),
        _s("rel_volume_20", "volume", "ratio", f_rel_volume, 20, 21,
           "V_t / mean(V_{t-20..t-1})", params={"n": 20}),
        _s("vt_anomaly_count_20", "volume", "count", f_vt_anomaly_count, 20, 20,
           "bars with turnover/volume outside [L,H] (unit/data anomaly)", params={"n": 20}),
        _s("clv_volume_pressure_proxy_20", "volume", "fraction", f_clv_volume_pressure, 20, 20,
           "Volume Pressure PROXY: volume-weighted close-location value, in [-1, 1]", params={"n": 20},
           is_proxy=True, proxy_for="cvd",
           limits="Not order flow. OHLCV cannot decompose buy/sell volume."),
        FeatureSpec(name="cvd", version=1, family="volume", unit="base",
                    description="True Cumulative Volume Delta (taker buy - taker sell volume)",
                    fn=None, lookback=1, min_observations=1, implemented=False,
                    not_implemented_reason="True CVD requires trade-level (taker side) data; "
                                           "OHLCV candles cannot provide it"),
        # structure
        _s("rolling_high_20", "structure", "quote", f_rolling_high, 20, 21, "max(H) of previous 20 bars",
           params={"n": 20}),
        _s("rolling_low_20", "structure", "quote", f_rolling_low, 20, 21, "min(L) of previous 20 bars",
           params={"n": 20}),
        _s("dist_high_20_pct", "structure", "fraction", f_dist_high, 20, 21,
           "C_t / rolling_high_20 - 1 (>0 = breakout above)", params={"n": 20}),
        _s("dist_low_20_pct", "structure", "fraction", f_dist_low, 20, 21,
           "C_t / rolling_low_20 - 1 (<0 = breakdown below)", params={"n": 20}),
        _s("range_position_20", "structure", "fraction", f_range_position, 20, 21,
           "(C_t - low20)/(high20 - low20), previous 20 bars; >1 or <0 = outside range", params={"n": 20}),
        _s("volume_concentration_20", "structure", "fraction", f_volume_concentration, 20, 20,
           "max(V)/sum(V) over 20 bars", params={"n": 20}),
        _s("amihud_20", "structure", "per_1m_quote", f_amihud, 20, 21,
           "Amihud illiquidity: mean(|log r|/turnover)*1e6", params={"n": 20}),
    ]
    return FeatureRegistry(specs)


DEFAULT_REGISTRY = build_default_registry()
