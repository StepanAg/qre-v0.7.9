"""Research entities. A Feature knows whether it is valid; consumers must never
read a feature that was computed on insufficient history (lesson: EMA/ATR on too
few candles, silent fallbacks, wrong field usage)."""
from __future__ import annotations

import math

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping

from app.domain.common import req, utc
from app.domain.enums import FeatureStatus, GapSeverity, QualityStatus
from app.domain.errors import DomainError
from app.domain.market import Symbol, Timeframe
from app.domain.regime import RegimeSnapshot


@dataclass(frozen=True)
class Feature:
    """Result of one feature at one point in time.

    value is a float (IEEE-754 double): features are statistics, not money
    (see docs/V070_FEATURE_METHODOLOGY.md). A value exists ONLY when
    status == VALUE; every other status carries a reason and no value, so a
    data problem can never be read as 0 or as a previous value."""
    name: str
    value: float | None
    timeframe: Timeframe
    as_of: datetime                     # decision time; only bars with close_time <= as_of were used
    status: FeatureStatus
    version: int = 1
    lookback: int = 0                   # formula parameter N (conceptual window)
    min_observations: int = 0           # contiguous closed bars the value needs (lookback + warmup)
    bars_used: int = 0
    unit: str = ""
    reason: str | None = None
    quality: QualityStatus = QualityStatus.VALID
    gap_severity: GapSeverity = GapSeverity.NO_GAP

    def __post_init__(self) -> None:
        req("name", self.name)
        object.__setattr__(self, "timeframe", Timeframe.parse(self.timeframe))
        utc("as_of", self.as_of)
        if self.status is FeatureStatus.VALUE:
            if self.value is None or isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
                raise DomainError(f"feature {self.name}: VALUE requires a numeric value")
            if not math.isfinite(self.value):
                raise DomainError(f"feature {self.name}: non-finite value must have status INVALID")
            if self.bars_used < self.min_observations:
                raise DomainError(f"feature {self.name}: VALUE with {self.bars_used}/"
                                  f"{self.min_observations} bars")
            object.__setattr__(self, "value", float(self.value))
        else:
            if self.value is not None:
                raise DomainError(f"feature {self.name}: status {self.status.value} must not carry a value")
            if not self.reason:
                raise DomainError(f"feature {self.name}: status {self.status.value} requires a reason")

    @property
    def is_valid(self) -> bool:
        return self.status is FeatureStatus.VALUE

    @property
    def key(self) -> str:
        return f"{self.name}:v{self.version}"


@dataclass(frozen=True)
class FeatureSnapshot:
    """All features of one symbol at one decision time. Keys are feature names
    for the base timeframe and "<tf>:<name>" for other timeframes."""
    symbol: Symbol
    as_of: datetime
    features: Mapping[str, Feature] = field(default_factory=dict)
    timeframe: Timeframe | None = None
    feature_set_hash: str = ""          # identifies names + versions + formulas
    input_fingerprint: str = ""         # identifies the exact input bars
    engine_version: str = ""

    def get(self, name: str) -> float:
        """Strict accessor: unknown or non-VALUE features raise instead of
        returning 0/None, so strategy code cannot silently use garbage."""
        if name not in self.features:
            raise KeyError(f"feature {name!r} not in snapshot (have: {sorted(self.features)})")
        f = self.features[name]
        if not f.is_valid or f.value is None:
            raise DomainError(f"feature {name!r} is {f.status.value}: {f.reason}")
        return f.value

    def is_ready(self, *names: str) -> bool:
        return all(n in self.features and self.features[n].is_valid for n in names)

    def failures(self) -> dict[str, Feature]:
        return {k: f for k, f in self.features.items() if not f.is_valid}


@dataclass(frozen=True)
class ResearchSnapshot:
    """DEPRECATED (Phase 9, decisions F2/D9-1): the Strategy port now takes a point-in-time
    SetupView of the Phase 6 setup; nothing uses this class. Kept physically.
    Original intent: immutable bundle a strategy (or AI) receives.
    Phase 3: the Phase 0 placeholder MarketState (with an undefined Decimal
    'confidence') is replaced by the explicit RegimeSnapshot."""
    snapshot_id: str
    features: FeatureSnapshot
    regime: RegimeSnapshot
