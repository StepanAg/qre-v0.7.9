"""Structure thresholds: versioned JSON, every key required, validated."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Mapping

from app.domain.enums import BreakMethod
from app.domain.errors import DomainError

_INTS = {"swing_left": (1, 20), "swing_right": (1, 20), "min_bars": (5, 10_000), "window_bars": (5, 10_000),
         "eq_max_separation_bars": (1, 10_000), "level_expiry_bars": (1, 100_000), "snapshot_max_swings": (1, 1000),
         "recent_events": (1, 1000), "recent_sweep_bars": (1, 100_000)}


@dataclass(frozen=True)
class StructureConfig:
    schema: int
    swing_left: int               # bars left of a pivot that must be strictly lower (highs) / higher (lows)
    swing_right: int              # bars right of a pivot = confirmation delay
    break_confirmation: BreakMethod
    min_bars: int                 # contiguous closed bars required at all
    window_bars: int              # structure memory: last N contiguous bars are analysed
    atr_feature: str              # Phase 2 feature used for EQH/EQL tolerance
    eq_tolerance_atr: float       # |price difference| <= eq_tolerance_atr * ATR(at formation)
    eq_max_separation_bars: int   # max distance between equal extremes
    level_expiry_bars: int        # ACTIVE level older than this -> EXPIRED
    snapshot_max_swings: int
    recent_events: int
    recent_sweep_bars: int

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "StructureConfig":
        names = {f.name for f in fields(cls)}
        data = {k: v for k, v in raw.items() if not k.startswith("_")}
        missing, extra = names - set(data), set(data) - names
        if missing or extra:
            raise DomainError(f"structure config keys: missing={sorted(missing)} unknown={sorted(extra)}")
        if data["schema"] != 1:
            raise DomainError(f"unsupported structure config schema {data['schema']}")
        vals: dict[str, Any] = {"schema": 1}
        for k, (lo, hi) in _INTS.items():
            v = data[k]
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise DomainError(f"structure config {k} must be an integer in [{lo}, {hi}], got {v!r}")
            vals[k] = v
        try:
            vals["break_confirmation"] = BreakMethod(data["break_confirmation"])
        except ValueError:
            raise DomainError(f"break_confirmation must be one of {[m.value for m in BreakMethod]}") from None
        t = data["eq_tolerance_atr"]
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 < t <= 5:
            raise DomainError("eq_tolerance_atr must be a number in (0, 5]")
        vals["eq_tolerance_atr"] = float(t)
        if not isinstance(data["atr_feature"], str):
            raise DomainError("atr_feature must be a feature name")
        from app.research.features.catalog import DEFAULT_REGISTRY
        try:
            spec = DEFAULT_REGISTRY.get(data["atr_feature"])
        except KeyError:
            raise DomainError(f"atr_feature {data['atr_feature']!r} is not a Phase 2 feature") from None
        if not spec.implemented or spec.is_proxy or spec.unit != "quote":
            raise DomainError("atr_feature must be an implemented, non-proxy feature in quote (price) units")
        vals["atr_feature"] = data["atr_feature"]
        cfg = cls(**vals)
        if cfg.min_bars < cfg.swing_left + cfg.swing_right + 1:
            raise DomainError("min_bars must cover at least one full swing (left + right + 1)")
        if cfg.window_bars < cfg.min_bars:
            raise DomainError("window_bars must be >= min_bars")
        if cfg.eq_max_separation_bars <= cfg.swing_left:
            raise DomainError("eq_max_separation_bars must exceed swing_left (two pivots cannot be closer)")
        return cfg

    @classmethod
    def from_file(cls, path: Path | str) -> "StructureConfig":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise DomainError(f"cannot read structure config {path}: {e}") from e
        return cls.from_mapping(raw)

    def canonical(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        d["break_confirmation"] = self.break_confirmation.value
        return d

    def with_(self, **changes) -> "StructureConfig":
        return StructureConfig.from_mapping({**self.canonical(), **changes})

    @property
    def config_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.canonical(), sort_keys=True).encode()).hexdigest()[:16]
