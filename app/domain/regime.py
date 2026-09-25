"""Regime snapshot model (Phase 3). Descriptive market state, not a forecast.

No probability/confidence field: there is no calibrated methodology for one.
Strength is categorical and defined by explicit rules (docs/V070_REGIME_ENGINE.md).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from app.domain.common import req, utc
from app.domain.enums import (Category, ContextStatus, MarketRegime, MtfAlignment, QualityStatus,
                              TrendDirection, TrendStrength, VolatilityState)
from app.domain.errors import DomainError
from app.domain.market import Symbol, Timeframe


@dataclass(frozen=True)
class TimeframeRegime:
    """Classification of ONE timeframe at one decision time."""
    timeframe: Timeframe
    regime: MarketRegime
    trend_direction: TrendDirection
    trend_strength: TrendStrength
    volatility_state: VolatilityState
    data_quality: QualityStatus
    data_through: datetime          # close time of the last bar this timeframe could use at as_of
    reason_codes: tuple[str, ...]
    metrics: Mapping[str, float] = field(default_factory=dict)   # normalised inputs actually used

    def __post_init__(self) -> None:
        object.__setattr__(self, "timeframe", Timeframe.parse(self.timeframe))
        utc("data_through", self.data_through)
        if not self.reason_codes:
            raise DomainError("every regime result needs at least one reason code")
        if self.regime is MarketRegime.UNKNOWN and not any(c.startswith("UNKNOWN:") for c in self.reason_codes):
            raise DomainError("UNKNOWN regime must carry an 'UNKNOWN:<tf>:<why>' reason code")
        if self.regime is not MarketRegime.UNKNOWN and self.data_quality is not QualityStatus.VALID:
            raise DomainError("a regime can only be classified on VALID data")

    def to_dict(self) -> dict:
        return {"timeframe": self.timeframe.value, "regime": self.regime.value,
                "trend_direction": self.trend_direction.value, "trend_strength": self.trend_strength.value,
                "volatility_state": self.volatility_state.value, "data_quality": self.data_quality.value,
                "data_through": self.data_through.isoformat(), "reason_codes": list(self.reason_codes),
                "metrics": dict(sorted(self.metrics.items()))}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "TimeframeRegime":
        return cls(Timeframe(d["timeframe"]), MarketRegime(d["regime"]), TrendDirection(d["trend_direction"]),
                   TrendStrength(d["trend_strength"]), VolatilityState(d["volatility_state"]),
                   QualityStatus(d["data_quality"]), datetime.fromisoformat(d["data_through"]),
                   tuple(d["reason_codes"]), dict(d["metrics"]))


@dataclass(frozen=True)
class BtcContext:
    """General market background from BTC. No correlation with the analysed
    symbol is assumed or computed here."""
    symbol: Symbol
    status: ContextStatus
    timeframe: Timeframe
    regime: MarketRegime
    trend_direction: TrendDirection
    volatility_state: VolatilityState
    data_quality: QualityStatus
    data_through: datetime | None
    reason_codes: tuple[str, ...]
    input_fingerprint: str = ""

    def __post_init__(self) -> None:
        if self.status in (ContextStatus.AVAILABLE, ContextStatus.SELF) and self.data_quality is not QualityStatus.VALID:
            raise DomainError("BTC context AVAILABLE requires VALID data")
        if self.status is ContextStatus.UNAVAILABLE and self.regime is not MarketRegime.UNKNOWN:
            raise DomainError("unavailable BTC context cannot carry a regime")

    def to_dict(self) -> dict:
        return {"symbol": self.symbol.name, "category": self.symbol.category.value, "status": self.status.value,
                "timeframe": self.timeframe.value, "regime": self.regime.value,
                "trend_direction": self.trend_direction.value, "volatility_state": self.volatility_state.value,
                "data_quality": self.data_quality.value,
                "data_through": self.data_through.isoformat() if self.data_through else None,
                "reason_codes": list(self.reason_codes), "input_fingerprint": self.input_fingerprint}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "BtcContext":
        return cls(Symbol(d["symbol"], Category(d["category"])), ContextStatus(d["status"]),
                   Timeframe(d["timeframe"]), MarketRegime(d["regime"]), TrendDirection(d["trend_direction"]),
                   VolatilityState(d["volatility_state"]), QualityStatus(d["data_quality"]),
                   datetime.fromisoformat(d["data_through"]) if d["data_through"] else None,
                   tuple(d["reason_codes"]), d.get("input_fingerprint", ""))


@dataclass(frozen=True)
class RegimeSnapshot:
    symbol: Symbol
    as_of: datetime                  # decision time (a close time of the base timeframe)
    timeframe: Timeframe             # base timeframe
    regime: MarketRegime             # = base timeframe regime (MTF conflicts are NOT hidden, see mtf_alignment)
    trend_direction: TrendDirection
    trend_strength: TrendStrength
    volatility_state: VolatilityState
    data_quality: QualityStatus      # of the base timeframe
    reason_codes: tuple[str, ...]
    timeframes: tuple[TimeframeRegime, ...]
    mtf_alignment: MtfAlignment
    btc_context: BtcContext | None
    feature_timestamps: Mapping[str, str]   # tf -> close time of last bar used (ISO)
    code_version: str
    feature_version: str             # feature engine version + feature_set_hash
    regime_version: str              # rules version
    config_hash: str                 # thresholds actually used
    input_fingerprint: str           # exact bars (symbol + BTC) the result depends on

    def __post_init__(self) -> None:
        utc("as_of", self.as_of)
        for n in ("code_version", "feature_version", "regime_version", "config_hash", "input_fingerprint"):
            req(n, getattr(self, n))
        if not self.timeframes or self.timeframes[0].timeframe is not self.timeframe:
            raise DomainError("timeframes[0] must be the base timeframe")
        base = self.timeframes[0]
        if (base.regime, base.trend_direction, base.data_quality) != \
                (self.regime, self.trend_direction, self.data_quality):
            raise DomainError("top-level regime must equal the base timeframe classification")

    @property
    def key(self) -> tuple:
        return (self.symbol.category.value, self.symbol.name, self.timeframe.value, self.as_of,
                self.regime_version, self.config_hash, self.input_fingerprint)

    def to_dict(self) -> dict:
        return {"symbol": self.symbol.name, "category": self.symbol.category.value,
                "as_of": self.as_of.isoformat(), "timeframe": self.timeframe.value, "regime": self.regime.value,
                "trend_direction": self.trend_direction.value, "trend_strength": self.trend_strength.value,
                "volatility_state": self.volatility_state.value, "data_quality": self.data_quality.value,
                "reason_codes": list(self.reason_codes), "timeframes": [t.to_dict() for t in self.timeframes],
                "mtf_alignment": self.mtf_alignment.value,
                "btc_context": self.btc_context.to_dict() if self.btc_context else None,
                "feature_timestamps": dict(sorted(self.feature_timestamps.items())),
                "code_version": self.code_version, "feature_version": self.feature_version,
                "regime_version": self.regime_version, "config_hash": self.config_hash,
                "input_fingerprint": self.input_fingerprint}

    def to_json(self) -> str:
        """Canonical JSON (sorted keys); floats use repr -> exact round-trip."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "RegimeSnapshot":
        return cls(Symbol(d["symbol"], Category(d["category"])), datetime.fromisoformat(d["as_of"]),
                   Timeframe(d["timeframe"]), MarketRegime(d["regime"]), TrendDirection(d["trend_direction"]),
                   TrendStrength(d["trend_strength"]), VolatilityState(d["volatility_state"]),
                   QualityStatus(d["data_quality"]), tuple(d["reason_codes"]),
                   tuple(TimeframeRegime.from_dict(t) for t in d["timeframes"]), MtfAlignment(d["mtf_alignment"]),
                   BtcContext.from_dict(d["btc_context"]) if d["btc_context"] else None,
                   dict(d["feature_timestamps"]), d["code_version"], d["feature_version"], d["regime_version"],
                   d["config_hash"], d["input_fingerprint"])

    @classmethod
    def from_json(cls, s: str) -> "RegimeSnapshot":
        return cls.from_dict(json.loads(s))
