"""RegimeEngine: FeatureSnapshot(s) -> RegimeSnapshot. Pure and deterministic.

Multi-timeframe: every timeframe is classified independently from features that
FeatureEngine prepared at the SAME decision time; a higher-timeframe bar that has
not closed is not part of its series, so no look-ahead is possible here.
Conflicts between timeframes are reported (mtf_alignment + reason codes), never
averaged away. The top-level regime is the base timeframe's own classification.
"""
from __future__ import annotations

import hashlib
from datetime import datetime

import app
from app.domain.enums import (ContextStatus, MarketRegime, MtfAlignment, QualityStatus, TrendDirection)
from app.domain.market import Symbol, Timeframe
from app.domain.regime import BtcContext, RegimeSnapshot, TimeframeRegime
from app.domain.research import FeatureSnapshot
from app.research.regime.config import RegimeConfig
from app.research.regime.rules import REGIME_VERSION, classify_timeframe, data_through

_TREND = {MarketRegime.TRENDING_UP: 1, MarketRegime.TRENDING_DOWN: -1}


def mtf_alignment(results: list[TimeframeRegime]) -> tuple[MtfAlignment, list[str]]:
    if len(results) == 1:
        return MtfAlignment.SINGLE, []
    codes: list[str] = []
    unknown = [r for r in results if r.regime is MarketRegime.UNKNOWN]
    known = [r for r in results if r.regime is not MarketRegime.UNKNOWN]
    dirs = {_TREND[r.regime] for r in known if r.regime in _TREND}
    if dirs == {1, -1}:
        desc = ",".join(f"{r.timeframe.value}={r.regime.value}" for r in known)
        codes.append(f"MTF_CONFLICT:{desc}")
        return MtfAlignment.CONFLICT, codes + [f"MTF_UNAVAILABLE:{r.timeframe.value}" for r in unknown]
    if unknown:
        codes += [f"MTF_UNAVAILABLE:{r.timeframe.value}:{r.data_quality.value}" for r in unknown]
        return MtfAlignment.PARTIAL, codes
    if len(dirs) == 1 and all(r.regime in _TREND for r in known):
        codes.append(f"MTF_ALIGNED:{known[0].regime.value}")
        return MtfAlignment.ALIGNED, codes
    codes.append("MTF_MIXED:" + ",".join(f"{r.timeframe.value}={r.regime.value}" for r in known))
    return MtfAlignment.MIXED, codes


def btc_context(cfg: RegimeConfig, symbol: Symbol, btc_snap: FeatureSnapshot | None,
                as_of: datetime, self_result: TimeframeRegime | None = None) -> BtcContext:
    tf = cfg.btc_timeframe
    if symbol == cfg.btc_symbol and self_result is not None:
        r = self_result
        status = ContextStatus.SELF if r.data_quality is QualityStatus.VALID else _status_for(r.data_quality)
        return _ctx(cfg, status, r, "self")
    if btc_snap is None:
        return BtcContext(cfg.btc_symbol, ContextStatus.UNAVAILABLE, tf, MarketRegime.UNKNOWN,
                          TrendDirection.UNKNOWN, _unknown_vol(), QualityStatus.MISSING_HISTORY, None,
                          ("BTC_CONTEXT_UNAVAILABLE:no_data",))
    r = classify_timeframe(btc_snap, tf, cfg)
    return _ctx(cfg, _status_for(r.data_quality), r, btc_snap.input_fingerprint)


def _unknown_vol():
    from app.domain.enums import VolatilityState
    return VolatilityState.UNKNOWN


def _status_for(q: QualityStatus) -> ContextStatus:
    if q is QualityStatus.VALID:
        return ContextStatus.AVAILABLE
    return ContextStatus.STALE if q is QualityStatus.STALE else ContextStatus.UNAVAILABLE


def _ctx(cfg: RegimeConfig, status: ContextStatus, r: TimeframeRegime, fp: str) -> BtcContext:
    usable = status in (ContextStatus.AVAILABLE, ContextStatus.SELF)
    codes = (f"BTC_CONTEXT_{status.value.upper()}",) + tuple(c for c in r.reason_codes if c.startswith(
        ("REGIME:", "UNKNOWN:", "REQ_FEATURE", "VOL_")))
    return BtcContext(cfg.btc_symbol, status, r.timeframe,
                      r.regime if usable else MarketRegime.UNKNOWN,
                      r.trend_direction if usable else TrendDirection.UNKNOWN,
                      r.volatility_state if usable else _unknown_vol(),
                      r.data_quality, r.data_through if usable else None, codes, fp)


class RegimeEngine:
    def __init__(self, config: RegimeConfig) -> None:
        self.config = config

    def classify(self, symbol: Symbol, snap: FeatureSnapshot, base: Timeframe,
                 timeframes: tuple[Timeframe, ...] | None = None,
                 btc_snap: FeatureSnapshot | None = None) -> RegimeSnapshot:
        """snap: FeatureEngine.snapshot_mtf result (base keys plain, others "<tf>:name").
        btc_snap: BTC FeatureSnapshot on config.btc_timeframe at the same as_of (or None)."""
        cfg = self.config
        tfs = timeframes or tuple(t for t in cfg.timeframes)
        if base not in tfs:
            raise ValueError(f"base timeframe {base.value} not in analysed timeframes")
        ordered = (base,) + tuple(t for t in tfs if t is not base)
        results = [classify_timeframe(snap, tf, cfg, prefix="" if tf is base else f"{tf.value}:")
                   for tf in ordered]
        base_r = results[0]
        align, mtf_codes = mtf_alignment(results)

        self_r = None
        if symbol == cfg.btc_symbol:
            self_r = next((r for r in results if r.timeframe is cfg.btc_timeframe), None)
            if self_r is None:
                raise ValueError("analysing BTC: include btc_timeframe in the analysed timeframes")
        ctx = btc_context(cfg, symbol, btc_snap, snap.as_of, self_r)
        if btc_snap is not None and btc_snap.as_of != snap.as_of:
            raise ValueError("BTC snapshot must be computed at the same decision time")

        codes = list(base_r.reason_codes) + mtf_codes + [ctx.reason_codes[0]]
        fp = hashlib.sha256(f"{snap.input_fingerprint}|{ctx.input_fingerprint}".encode()).hexdigest()[:24]
        return RegimeSnapshot(
            symbol=symbol, as_of=snap.as_of, timeframe=base, regime=base_r.regime,
            trend_direction=base_r.trend_direction, trend_strength=base_r.trend_strength,
            volatility_state=base_r.volatility_state, data_quality=base_r.data_quality,
            reason_codes=tuple(codes), timeframes=tuple(results), mtf_alignment=align, btc_context=ctx,
            feature_timestamps={r.timeframe.value: data_through(r.timeframe, snap.as_of).isoformat()
                                for r in results},
            code_version=app.__version__, feature_version=f"engine{snap.engine_version}:{snap.feature_set_hash}",
            regime_version=REGIME_VERSION, config_hash=cfg.config_hash, input_fingerprint=fp)
