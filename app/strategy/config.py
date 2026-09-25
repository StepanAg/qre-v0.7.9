"""Strategy v1 configuration: versioned JSON, every key required, validated."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Mapping

from app.domain.enums import SetupType
from app.domain.errors import DomainError

ENTRY_ON = ("candidate", "confirmed")
FITS = ("allowed", "not_allowed", "unknown")


@dataclass(frozen=True)
class StrategyConfig:
    schema: int
    strategy_id: str
    setup_types: tuple[str, ...]
    entry_on: str                       # the lifecycle fact that makes a decision possible
    regime_fit_allowed: tuple[str, ...]
    reject_conflicting_setups: bool     # opposite-direction setups active at decision time
    stop_buffer_atr: float
    take_profit_r: float
    max_hold_bars: int

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "StrategyConfig":
        names = {f.name for f in fields(cls)}
        d = {k: v for k, v in raw.items() if not k.startswith("_")}
        missing, extra = names - set(d), set(d) - names
        if missing or extra:
            raise DomainError(f"strategy config keys: missing={sorted(missing)} unknown={sorted(extra)}")
        if d["schema"] != 1:
            raise DomainError(f"unsupported strategy config schema {d['schema']}")
        if not isinstance(d["strategy_id"], str) or not d["strategy_id"]:
            raise DomainError("strategy_id required")
        try:
            types = tuple(SetupType(t).value for t in d["setup_types"])
        except ValueError as e:
            raise DomainError(f"strategy config: {e}") from None
        if not types or len(set(types)) != len(types):
            raise DomainError("setup_types must be non-empty and unique")
        if d["entry_on"] not in ENTRY_ON:
            raise DomainError(f"entry_on must be one of {ENTRY_ON}")
        fits = tuple(d["regime_fit_allowed"])
        if not fits or any(f not in FITS for f in fits):
            raise DomainError(f"regime_fit_allowed must be a non-empty subset of {FITS}")
        if not isinstance(d["reject_conflicting_setups"], bool):
            raise DomainError("reject_conflicting_setups must be true or false")
        for k, lo, hi in (("stop_buffer_atr", 0, 10), ("take_profit_r", 0, 100)):
            v = d[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not (lo <= v <= hi) \
                    or (k == "take_profit_r" and v == 0):
                raise DomainError(f"{k} out of range: {v!r}")
        m = d["max_hold_bars"]
        if isinstance(m, bool) or not isinstance(m, int) or not 1 <= m <= 100_000:
            raise DomainError("max_hold_bars must be an integer in [1, 100000]")
        return cls(1, d["strategy_id"], types, d["entry_on"], fits, d["reject_conflicting_setups"],
                   float(d["stop_buffer_atr"]), float(d["take_profit_r"]), m)

    @classmethod
    def from_file(cls, path) -> "StrategyConfig":
        try:
            return cls.from_mapping(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as e:
            raise DomainError(f"cannot read strategy config {path}: {e}") from e

    def canonical(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        d["setup_types"], d["regime_fit_allowed"] = list(self.setup_types), list(self.regime_fit_allowed)
        return d

    def with_(self, **ch) -> "StrategyConfig":
        return StrategyConfig.from_mapping({**self.canonical(), **ch})

    @property
    def config_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.canonical(), sort_keys=True).encode()).hexdigest()[:16]
