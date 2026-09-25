"""Monitor configuration: versioned JSON file, every key required (no hidden defaults)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Mapping

from app.domain.errors import DomainError
from app.domain.market import Symbol, Timeframe

_INT_FIELDS = {"poll_interval_s": (1, 3600), "bar_close_grace_s": (0, 600), "stale_after_s": (1, 86_400),
               "heartbeat_interval_s": (1, 3600), "unit_max_attempts": (1, 50),
               "unit_backoff_base_s": (1, 3600), "unit_backoff_max_s": (1, 86_400),
               "api_down_after_failures": (1, 100)}


@dataclass(frozen=True)
class MonitorConfig:
    schema: int
    symbols: tuple[Symbol, ...]
    timeframes: tuple[Timeframe, ...]
    poll_interval_s: int          # how often the loop wakes up (not how often it analyses)
    bar_close_grace_s: int        # wait after a bar close before expecting it via REST
    stale_after_s: int            # expected bar still missing this long after grace -> STALE
    heartbeat_interval_s: int
    unit_max_attempts: int        # attempts per (symbol, timeframe, bar) before RETRY_EXHAUSTED
    unit_backoff_base_s: int      # unit backoff: base * 2^(n-1), capped
    unit_backoff_max_s: int
    api_down_after_failures: int  # consecutive failed API operations -> api_status DOWN
    max_runtime_minutes: int | None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "MonitorConfig":
        names = {f.name for f in fields(cls)}
        data = {k: v for k, v in raw.items() if not k.startswith("_")}
        missing, extra = names - set(data), set(data) - names
        if missing or extra:
            raise DomainError(f"monitor config keys: missing={sorted(missing)} unknown={sorted(extra)}")
        if data["schema"] != 1:
            raise DomainError(f"unsupported monitor config schema {data['schema']}")
        vals: dict[str, Any] = {"schema": 1}
        for k, (lo, hi) in _INT_FIELDS.items():
            v = data[k]
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise DomainError(f"monitor config {k} must be an integer in [{lo}, {hi}], got {v!r}")
            vals[k] = v
        mr = data["max_runtime_minutes"]
        if mr is not None and (isinstance(mr, bool) or not isinstance(mr, int) or mr < 1):
            raise DomainError("max_runtime_minutes must be null or a positive integer")
        vals["max_runtime_minutes"] = mr
        if not data["symbols"] or len(set(data["symbols"])) != len(data["symbols"]):
            raise DomainError("symbols must be non-empty and unique")
        vals["symbols"] = tuple(Symbol(s) for s in data["symbols"])
        vals["timeframes"] = tuple(sorted({Timeframe.parse(t) for t in data["timeframes"]}, key=lambda t: t.ms))
        if len(vals["timeframes"]) != len(data["timeframes"]) or not vals["timeframes"]:
            raise DomainError("timeframes must be non-empty and unique")
        cfg = cls(**vals)
        if cfg.unit_backoff_max_s < cfg.unit_backoff_base_s:
            raise DomainError("unit_backoff_max_s must be >= unit_backoff_base_s")
        if cfg.poll_interval_s > cfg.timeframes[0].ms // 1000:
            raise DomainError("poll_interval_s longer than the smallest timeframe would skip bars")
        return cfg

    @classmethod
    def from_file(cls, path: Path | str) -> "MonitorConfig":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise DomainError(f"cannot read monitor config {path}: {e}") from e
        return cls.from_mapping(raw)

    def with_overrides(self, *, symbols=None, timeframes=None, max_runtime_minutes=None) -> "MonitorConfig":
        raw = self.canonical()
        if symbols:
            raw["symbols"] = list(symbols)
        if timeframes:
            raw["timeframes"] = list(timeframes)
        if max_runtime_minutes is not None:
            raw["max_runtime_minutes"] = max_runtime_minutes
        return MonitorConfig.from_mapping(raw)

    def canonical(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        d["symbols"] = [s.name for s in self.symbols]
        d["timeframes"] = [t.value for t in self.timeframes]
        return d

    @property
    def config_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.canonical(), sort_keys=True).encode()).hexdigest()[:16]

    def backoff_s(self, failures: int) -> int:
        """Unit backoff after `failures` consecutive failures (>= 1)."""
        return min(self.unit_backoff_max_s, self.unit_backoff_base_s * 2 ** (max(failures, 1) - 1))
