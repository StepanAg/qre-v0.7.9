"""Regime v1 rules: one timeframe, one FeatureSnapshot, pure and deterministic.

Pipeline (docs/V070_REGIME_ENGINE.md):
 1. data quality gate   - every REQUIRED feature must be VALUE, else UNKNOWN
 2. normalise to ATRs   - scale-free across symbols and timeframes
 3. volatility state    - vol_ratio_20_100 vs LOW/HIGH thresholds
 4. component votes     - structure, momentum, displacement (+ optional horizon) in {-1, 0, +1}
 5. regime              - HIGH_VOLATILITY > TRENDING_* > RANGING > TRANSITION
"""
from __future__ import annotations

from datetime import datetime

from app.domain.enums import (FeatureStatus, MarketRegime, QualityStatus, TrendDirection, TrendStrength,
                              VolatilityState)
from app.domain.regime import TimeframeRegime
from app.domain.research import Feature, FeatureSnapshot
from app.domain.market import Timeframe
from app.research.regime.config import OPTIONAL_FEATURES, REQUIRED_FEATURES, RegimeConfig

REGIME_VERSION = "1"

# Worst first: the reported data quality is the most severe among failed required features.
_QUALITY_PRIORITY = (QualityStatus.DUPLICATE, QualityStatus.INVALID, QualityStatus.STALE, QualityStatus.GAP,
                     QualityStatus.MISSING_HISTORY, QualityStatus.INSUFFICIENT_HISTORY, QualityStatus.OPEN_CANDLE)


def _feature_quality(f: Feature) -> QualityStatus:
    if f.status is FeatureStatus.INSUFFICIENT_HISTORY:
        return f.quality if f.quality is QualityStatus.MISSING_HISTORY else QualityStatus.INSUFFICIENT_HISTORY
    if f.status is FeatureStatus.INVALID:
        return QualityStatus.INVALID
    if f.status is FeatureStatus.NOT_AVAILABLE:
        return QualityStatus.INVALID
    return f.quality if f.quality is not QualityStatus.VALID else QualityStatus.INVALID


_VOTE_NAME = {1: "UP", -1: "DOWN", 0: "FLAT"}


def _vote(x: float, threshold: float) -> int:
    """+1 / -1 beyond the neutral band [-threshold, +threshold], else 0."""
    if x >= threshold:
        return 1
    if x <= -threshold:
        return -1
    return 0


def data_through(tf: Timeframe, as_of: datetime) -> datetime:
    """Close time of the last bar of `tf` that is closed at `as_of`."""
    return tf.floor(as_of)


def classify_timeframe(snap: FeatureSnapshot, tf: Timeframe, cfg: RegimeConfig, *,
                       prefix: str = "") -> TimeframeRegime:
    """prefix: "" for the snapshot's base timeframe, "1h:" etc. for others
    (FeatureSnapshot key convention of FeatureEngine.snapshot_mtf)."""
    through = data_through(tf, snap.as_of)
    tag = tf.value
    unknown = dict(regime=MarketRegime.UNKNOWN, trend_direction=TrendDirection.UNKNOWN,
                   trend_strength=TrendStrength.UNKNOWN, volatility_state=VolatilityState.UNKNOWN)

    # 1. data-quality gate -----------------------------------------------------
    codes: list[str] = []
    bad: list[QualityStatus] = []
    for name in REQUIRED_FEATURES:
        f = snap.features.get(prefix + name)
        if f is None:
            codes.append(f"REQ_FEATURE_MISSING:{tag}:{name}")
            bad.append(QualityStatus.INVALID)
        elif not f.is_valid:
            codes.append(f"REQ_FEATURE_UNAVAILABLE:{tag}:{name}:{f.status.value}:{f.quality.value}")
            bad.append(_feature_quality(f))
    if bad:
        worst = min(bad, key=_QUALITY_PRIORITY.index)
        return TimeframeRegime(tf, data_quality=worst, data_through=through,
                               reason_codes=(f"UNKNOWN:{tag}:DATA_QUALITY_{worst.value.upper()}", *codes), **unknown)
    opt: dict[str, float] = {}
    for name in OPTIONAL_FEATURES:
        f = snap.features.get(prefix + name)
        if f is not None and f.is_valid:
            opt[name] = f.value  # type: ignore[assignment]
        else:
            codes.append(f"OPT_FEATURE_UNAVAILABLE:{tag}:{name}:{f.status.value if f else 'missing'}")

    g = lambda n: snap.get(prefix + n)  # noqa: E731  strict accessor
    atr_pct = g("atr_14_pct")
    if atr_pct <= 0:
        return TimeframeRegime(tf, data_quality=QualityStatus.VALID, data_through=through,
                               reason_codes=(f"UNKNOWN:{tag}:ATR_ZERO", *codes), **unknown)

    # 2. normalise to ATR units --------------------------------------------------
    m = {"spread_atr": g("ema_50_200_spread") / atr_pct,
         "slope_atr": g("ema_50_slope_10") / atr_pct,
         "displacement_atr": g("dist_ema_50_pct") / atr_pct,
         "vol_ratio": g("vol_ratio_20_100"),
         "atr_pct": atr_pct}
    if "ret_20" in opt:
        m["return_atr"] = opt["ret_20"] / atr_pct
    if "vol_20_annualized" in opt:
        m["vol_annualized"] = opt["vol_20_annualized"]          # reported only, not a rule input
    if "range_position_20" in opt:
        m["range_position"] = opt["range_position_20"]          # reported only, not a rule input

    # 3. volatility state ------------------------------------------------------------
    if m["vol_ratio"] >= cfg.vol_ratio_high:
        vol = VolatilityState.HIGH
    elif m["vol_ratio"] <= cfg.vol_ratio_low:
        vol = VolatilityState.LOW
    else:
        vol = VolatilityState.NORMAL
    codes.insert(0, f"VOL_{vol.value.upper()}:{tag}:ratio={m['vol_ratio']:.3f}")

    # 4. votes ------------------------------------------------------------------------
    votes = {"STRUCTURE": _vote(m["spread_atr"], cfg.structure_min_atr),
             "MOMENTUM": _vote(m["slope_atr"], cfg.slope_min_atr),
             "DISPLACEMENT": _vote(m["displacement_atr"], cfg.displacement_min_atr)}
    if "return_atr" in m:
        votes["HORIZON"] = _vote(m["return_atr"], cfg.return_min_atr)
    for k, v in votes.items():
        codes.append(f"{k}_{_VOTE_NAME[v]}:{tag}")
    signs = {v for v in votes.values() if v != 0}
    if signs == {1, -1}:
        direction = TrendDirection.CONFLICT
    elif sum(1 for v in votes.values() if v == 1) >= 2 and -1 not in signs:
        direction = TrendDirection.UP
    elif sum(1 for v in votes.values() if v == -1) >= 2 and 1 not in signs:
        direction = TrendDirection.DOWN
    else:
        direction = TrendDirection.NEUTRAL

    core = (votes["STRUCTURE"], votes["MOMENTUM"], votes["DISPLACEMENT"])
    horizon = votes.get("HORIZON")
    trending_up = core == (1, 1, 1) and horizon != -1
    trending_down = core == (-1, -1, -1) and horizon != 1

    # 5. regime -----------------------------------------------------------------------
    if vol is VolatilityState.HIGH:
        regime = MarketRegime.HIGH_VOLATILITY
        codes.insert(1, f"HIGH_VOL_OVERRIDES_TREND:{tag}")
    elif trending_up:
        regime = MarketRegime.TRENDING_UP
    elif trending_down:
        regime = MarketRegime.TRENDING_DOWN
    elif votes["STRUCTURE"] == 0 and votes["MOMENTUM"] == 0:
        regime = MarketRegime.RANGING
    else:
        regime = MarketRegime.TRANSITION
        why = "COMPONENTS_CONFLICT" if direction is TrendDirection.CONFLICT else "COMPONENTS_INCOMPLETE"
        codes.insert(1, f"TRANSITION:{tag}:{why}")

    if regime in (MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN):
        strong = abs(m["slope_atr"]) >= cfg.strong_slope_atr and horizon is not None and horizon != 0
        strength = TrendStrength.STRONG if strong else TrendStrength.MODERATE
    else:
        strength = TrendStrength.NONE
    codes.insert(0, f"REGIME:{tag}:{regime.value}")
    return TimeframeRegime(tf, regime, direction, strength, vol, QualityStatus.VALID, through, tuple(codes),
                           {k: float(v) for k, v in m.items()})
