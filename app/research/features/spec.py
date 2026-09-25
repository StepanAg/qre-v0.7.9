"""FeatureSpec (metadata + requirements + formula) and FeatureRegistry."""
from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from app.domain.market import Timeframe


def _formula_source(fn: Callable[..., Any], depth: int = 3, seen: set | None = None) -> str:
    """Source of fn plus every project function it calls (transitively, depth-
    limited), so editing a shared helper (e.g. ema_last) also changes the hash."""
    seen = seen if seen is not None else set()
    if fn in seen or depth < 0:
        return ""
    seen.add(fn)
    parts = [inspect.getsource(fn)]
    for name in sorted(fn.__code__.co_names):
        ref = fn.__globals__.get(name)
        if inspect.isfunction(ref) and ref.__module__.startswith("app."):
            parts.append(_formula_source(ref, depth - 1, seen))
    return "\n".join(parts)


@dataclass(frozen=True)
class Window:
    """Exactly the bars a formula may see: the last `min_observations`
    contiguous closed bars ending at the decision time. Oldest first."""
    timeframe: Timeframe
    o: list[float]
    h: list[float]
    l: list[float]
    c: list[float]
    v: list[float]
    q: list[float]

    def __len__(self) -> int:
        return len(self.c)


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    version: int
    family: str                       # returns | trend | volatility | volume | structure
    unit: str                         # e.g. "quote", "base", "fraction", "ratio", "count"
    description: str
    fn: Callable[..., Any] | None     # fn(window, **params) -> float | NotAvailable
    lookback: int                     # formula parameter N
    min_observations: int             # contiguous closed bars required (lookback + warmup + extra)
    warmup: int = 0
    params: Mapping[str, Any] = field(default_factory=dict)
    requires_closed: bool = True
    max_gap_bars: int = 0             # must be 0 in v1: strict contiguity (see gap policy)
    gap_rationale: str = ""
    is_proxy: bool = False
    proxy_for: str | None = None
    implemented: bool = True
    not_implemented_reason: str = ""
    interpretation: str = ""
    limitations: str = ""

    def __post_init__(self) -> None:
        if not self.name.isidentifier() or self.name != self.name.lower():
            raise ValueError(f"feature name must be lower_snake_case: {self.name!r}")
        if self.version < 1:
            raise ValueError("version must be >= 1")
        if self.implemented and self.fn is None:
            raise ValueError(f"{self.name}: implemented feature needs fn")
        if self.min_observations < max(1, self.lookback):
            raise ValueError(f"{self.name}: min_observations < lookback")
        if self.max_gap_bars != 0:
            # Window carries no gap markers, so a formula would treat bars on both
            # sides of a gap as adjacent (e.g. a "1-bar" return spanning 3 hours).
            # That is silent series gluing -> forbidden until Window exposes gaps.
            raise ValueError(f"{self.name}: gap tolerance (max_gap_bars>0) is not supported in v1; "
                             "it would glue the series across the gap")
        if self.is_proxy and not self.proxy_for:
            raise ValueError(f"{self.name}: proxy must say what it approximates")
        if not self.requires_closed:
            raise ValueError(f"{self.name}: open-candle features are not allowed in this engine")

    @property
    def key(self) -> str:
        return f"{self.name}:v{self.version}"

    @property
    def formula_hash(self) -> str:
        """Hash of the formula source + parameters. Changing the formula without
        bumping `version` is detected by tests against VERSIONS.lock."""
        src = _formula_source(self.fn) if self.fn is not None else "not-implemented"
        blob = json.dumps({"src": src, "params": dict(self.params), "min_obs": self.min_observations},
                          sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def metadata(self) -> dict:
        return {"name": self.name, "version": self.version, "family": self.family, "unit": self.unit,
                "lookback": self.lookback, "warmup": self.warmup, "min_observations": self.min_observations,
                "params": dict(self.params), "requires_closed": self.requires_closed,
                "max_gap_bars": self.max_gap_bars, "is_proxy": self.is_proxy, "proxy_for": self.proxy_for,
                "implemented": self.implemented, "formula_hash": self.formula_hash,
                "description": self.description}


class FeatureRegistry:
    """name -> versions. get(name) returns the latest version; get("name@2") a
    specific one. Registering the same (name, version) twice is an error."""

    def __init__(self, specs: Iterable[FeatureSpec] = ()) -> None:
        self._specs: dict[str, dict[int, FeatureSpec]] = {}
        for s in specs:
            self.register(s)

    def register(self, spec: FeatureSpec) -> None:
        versions = self._specs.setdefault(spec.name, {})
        if spec.version in versions:
            raise ValueError(f"{spec.key} already registered")
        versions[spec.version] = spec

    def get(self, ref: str) -> FeatureSpec:
        name, _, ver = ref.partition("@")
        if name not in self._specs:
            raise KeyError(f"unknown feature {name!r}")
        versions = self._specs[name]
        if ver:
            v = int(ver.lstrip("v"))
            if v not in versions:
                raise KeyError(f"{name} has no version {v} (have {sorted(versions)})")
            return versions[v]
        return versions[max(versions)]

    def latest(self) -> list[FeatureSpec]:
        return [v[max(v)] for _, v in sorted(self._specs.items())]

    def all_versions(self) -> list[FeatureSpec]:
        return [s for _, v in sorted(self._specs.items()) for _, s in sorted(v.items())]

    def names(self) -> list[str]:
        return sorted(self._specs)
