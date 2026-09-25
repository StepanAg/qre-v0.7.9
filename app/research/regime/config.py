"""Regime thresholds. The ONLY source is a versioned JSON file (config/regime_v1.json);
there are no in-code defaults, so a threshold can never silently differ from the file."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Mapping

from app.domain.errors import DomainError
from app.domain.market import Symbol, Timeframe

# Features the rules READ. Required: without them no classification (UNKNOWN).
# Optional: used when VALUE; their absence is recorded, never replaced.
REQUIRED_FEATURES = ("ema_50_200_spread", "ema_50_slope_10", "dist_ema_50_pct", "atr_14_pct", "vol_ratio_20_100")
OPTIONAL_FEATURES = ("ret_20", "vol_20_annualized", "range_position_20")
ALL_FEATURES = REQUIRED_FEATURES + OPTIONAL_FEATURES


@dataclass(frozen=True)
class RegimeConfig:
    schema: int
    structure_min_atr: float     # |EMA50-EMA200| in ATRs to count as a trend STRUCTURE
    slope_min_atr: float         # EMA50 move over 10 bars in ATRs to count as MOMENTUM
    displacement_min_atr: float  # |close-EMA50| in ATRs to count as DISPLACEMENT
    return_min_atr: float        # |20-bar return| in ATRs for the optional horizon vote
    strong_slope_atr: float      # slope (ATRs / 10 bars) that makes a trend STRONG
    vol_ratio_high: float        # std20/std100 at or above -> HIGH volatility
    vol_ratio_low: float         # std20/std100 at or below -> LOW volatility
    timeframes: tuple[Timeframe, ...]
    btc_symbol: Symbol
    btc_timeframe: Timeframe

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "RegimeConfig":
        names = {f.name for f in fields(cls)}
        data = {k: v for k, v in raw.items() if not k.startswith("_")}
        missing, extra = names - set(data), set(data) - names
        if missing or extra:
            raise DomainError(f"regime config keys: missing={sorted(missing)} unknown={sorted(extra)}")
        if data["schema"] != 1:
            raise DomainError(f"unsupported regime config schema {data['schema']}")
        vals: dict[str, Any] = {"schema": 1}
        for f in fields(cls):
            if f.type == "float":
                v = data[f.name]
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
                    raise DomainError(f"regime config {f.name} must be a positive number, got {v!r}")
                vals[f.name] = float(v)
        vals["timeframes"] = tuple(Timeframe.parse(t) for t in data["timeframes"])
        vals["btc_symbol"] = Symbol(data["btc_symbol"])
        vals["btc_timeframe"] = Timeframe.parse(data["btc_timeframe"])
        cfg = cls(**vals)
        cfg.validate()
        return cfg

    @classmethod
    def from_file(cls, path: Path | str) -> "RegimeConfig":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise DomainError(f"cannot read regime config {path}: {e}") from e
        return cls.from_mapping(raw)

    def validate(self) -> None:
        if not self.vol_ratio_low < 1.0 < self.vol_ratio_high:
            raise DomainError("need vol_ratio_low < 1 < vol_ratio_high")
        if self.strong_slope_atr < self.slope_min_atr:
            raise DomainError("strong_slope_atr must be >= slope_min_atr")
        if not self.timeframes or len(set(self.timeframes)) != len(self.timeframes):
            raise DomainError("timeframes must be non-empty and unique")
        if list(self.timeframes) != sorted(self.timeframes, key=lambda t: t.ms):
            raise DomainError("timeframes must be ordered from lowest to highest")

    def canonical(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        d["timeframes"] = [t.value for t in self.timeframes]
        d["btc_symbol"] = self.btc_symbol.name
        d["btc_timeframe"] = self.btc_timeframe.value
        return d

    @property
    def config_hash(self) -> str:
        blob = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:16]
