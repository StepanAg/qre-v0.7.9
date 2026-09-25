"""Setup thresholds: versioned JSON, every key required, validated."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Mapping

from app.domain.enums import MarketRegime, SetupType
from app.domain.errors import DomainError
from app.domain.market import Timeframe

SWEEP_CONFIRMATIONS = ("close_beyond_sweep_bar", "opposite_structure_break")
_FLOATS = {"pullback_zone_atr": (0, 5), "range_min_width_atr": (0, 100), "range_max_width_atr": (0, 1000),
           "range_touch_atr": (0, 5), "rejection_close_fraction": (0, 1)}
_INTS = {"ttl_bars": (1, 500), "recent_closed_bars": (0, 500), "breakout_hold_bars": (1, 50)}


@dataclass(frozen=True)
class SetupConfig:
    schema: int
    enabled_types: tuple[SetupType, ...]
    timeframes: tuple[Timeframe, ...]
    allowed_regimes: Mapping[SetupType, frozenset[MarketRegime]]
    atr_feature: str
    ttl_bars: int                  # a setup's life (bars after it became a candidate); pullback arm window too
    recent_closed_bars: int        # closed setups stay visible in snapshots this long
    breakout_hold_bars: int        # closes beyond the broken level needed to confirm a breakout
    pullback_zone_atr: float       # |price - broken level| <= zone * ATR counts as a return to the level
    sweep_confirmation: str        # close_beyond_sweep_bar | opposite_structure_break
    range_min_width_atr: float     # range top-bottom distance bounds, in ATR
    range_max_width_atr: float
    range_touch_atr: float         # how close to a boundary a bar must trade (without piercing it)
    rejection_close_fraction: float  # close in the lower (upper) fraction of the bar = rejection

    @property
    def lookback_bars(self) -> int:
        """Bars replayed per evaluation. 2*ttl covers a pullback armed ttl bars before its
        candidate; + recent_closed keeps every setup shown at as_of identical across evaluations."""
        return 2 * self.ttl_bars + self.recent_closed_bars

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "SetupConfig":
        names = {f.name for f in fields(cls)}
        data = {k: v for k, v in raw.items() if not k.startswith("_")}
        missing, extra = names - set(data), set(data) - names
        if missing or extra:
            raise DomainError(f"setup config keys: missing={sorted(missing)} unknown={sorted(extra)}")
        if data["schema"] != 1:
            raise DomainError(f"unsupported setup config schema {data['schema']}")
        v: dict[str, Any] = {"schema": 1}
        try:
            v["enabled_types"] = tuple(SetupType(t) for t in data["enabled_types"])
            v["timeframes"] = tuple(Timeframe.parse(t) for t in data["timeframes"])
            ar = data["allowed_regimes"]
            if not isinstance(ar, dict):
                raise DomainError("allowed_regimes must be an object")
            v["allowed_regimes"] = {SetupType(k): frozenset(MarketRegime(r) for r in vals) for k, vals in ar.items()}
        except ValueError as e:
            raise DomainError(f"setup config: {e}") from None
        if not v["enabled_types"] or len(set(v["enabled_types"])) != len(v["enabled_types"]):
            raise DomainError("enabled_types must be non-empty and unique")
        if not v["timeframes"]:
            raise DomainError("timeframes must be non-empty")
        if set(v["allowed_regimes"]) != set(SetupType):
            raise DomainError("allowed_regimes must list every setup type")
        if any(MarketRegime.UNKNOWN in rs for rs in v["allowed_regimes"].values()):
            raise DomainError("UNKNOWN cannot be an allowed regime")
        for k, (lo, hi) in _INTS.items():
            x = data[k]
            if isinstance(x, bool) or not isinstance(x, int) or not lo <= x <= hi:
                raise DomainError(f"setup config {k} must be an integer in [{lo}, {hi}]")
            v[k] = x
        for k, (lo, hi) in _FLOATS.items():
            x = data[k]
            if isinstance(x, bool) or not isinstance(x, (int, float)) or not lo < x <= hi:
                raise DomainError(f"setup config {k} must be a number in ({lo}, {hi}]")
            v[k] = float(x)
        if data["sweep_confirmation"] not in SWEEP_CONFIRMATIONS:
            raise DomainError(f"sweep_confirmation must be one of {SWEEP_CONFIRMATIONS}")
        v["sweep_confirmation"] = data["sweep_confirmation"]
        from app.research.features.catalog import DEFAULT_REGISTRY
        try:
            spec = DEFAULT_REGISTRY.get(data["atr_feature"])
        except KeyError:
            raise DomainError(f"atr_feature {data['atr_feature']!r} is not a Phase 2 feature") from None
        if not spec.implemented or spec.is_proxy or spec.unit != "quote":
            raise DomainError("atr_feature must be an implemented, non-proxy feature in price units")
        v["atr_feature"] = data["atr_feature"]
        cfg = cls(**v)
        if cfg.range_max_width_atr <= cfg.range_min_width_atr:
            raise DomainError("range_max_width_atr must be > range_min_width_atr")
        if cfg.range_touch_atr >= cfg.range_min_width_atr:
            raise DomainError("range_touch_atr must be smaller than range_min_width_atr")
        return cfg

    @classmethod
    def from_file(cls, path: Path | str) -> "SetupConfig":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise DomainError(f"cannot read setup config {path}: {e}") from e
        return cls.from_mapping(raw)

    def canonical(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        d["enabled_types"] = [t.value for t in self.enabled_types]
        d["timeframes"] = [t.value for t in self.timeframes]
        d["allowed_regimes"] = {k.value: sorted(r.value for r in rs) for k, rs in self.allowed_regimes.items()}
        return d

    def with_(self, **changes) -> "SetupConfig":
        return SetupConfig.from_mapping({**self.canonical(), **changes})

    @property
    def config_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.canonical(), sort_keys=True).encode()).hexdigest()[:16]
