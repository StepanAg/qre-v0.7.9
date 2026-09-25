"""Research and backtest configuration: versioned JSON, every key required."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Mapping

from app.domain.enums import RegimeFit, SetupType
from app.domain.errors import DomainError

FUNDING_MODES = ("data", "assumed", "data_or_assumed", "none")
ANCHORS = ("candidate", "confirmed")


def _load(path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise DomainError(f"cannot read config {path}: {e}") from e


def _keys(cls, raw: Mapping[str, Any]) -> dict:
    names = {f.name for f in fields(cls)}
    data = {k: v for k, v in raw.items() if not k.startswith("_")}
    missing, extra = names - set(data), set(data) - names
    if missing or extra:
        raise DomainError(f"{cls.__name__} keys: missing={sorted(missing)} unknown={sorted(extra)}")
    if data["schema"] != 1:
        raise DomainError(f"unsupported {cls.__name__} schema {data['schema']}")
    return data


def _num(name, v, lo, hi, *, integer=False, lo_open=True) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (integer and not isinstance(v, int)):
        raise DomainError(f"{name} must be {'an integer' if integer else 'a number'}")
    if (v <= lo if lo_open else v < lo) or v > hi:
        raise DomainError(f"{name} out of range: {v}")
    return v


def _hash(d: Mapping) -> str:
    return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class ResearchConfig:
    schema: int
    outcome_horizons: tuple[int, ...]     # bars after the anchor
    outcome_anchor: str                   # candidate | confirmed
    outcome_thresholds_atr: tuple[float, ...]
    split_fractions: tuple[float, float, float]   # train / validation / test, chronological
    embargo_bars: int

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ResearchConfig":
        d = _keys(cls, raw)
        hs = d["outcome_horizons"]
        if not hs or sorted(set(hs)) != list(hs):
            raise DomainError("outcome_horizons must be strictly increasing and non-empty")
        for h in hs:
            _num("outcome_horizons", h, 0, 10_000, integer=True)
        if d["outcome_anchor"] not in ANCHORS:
            raise DomainError(f"outcome_anchor must be one of {ANCHORS}")
        th = d["outcome_thresholds_atr"]
        for x in th:
            _num("outcome_thresholds_atr", x, 0, 100)
        fr = d["split_fractions"]
        if len(fr) != 3 or any(_num("split_fractions", x, 0, 1) is None for x in fr) or abs(sum(fr) - 1) > 1e-9:
            raise DomainError("split_fractions must be three positive numbers summing to 1")
        _num("embargo_bars", d["embargo_bars"], -1, 100_000, integer=True)
        return cls(1, tuple(hs), d["outcome_anchor"], tuple(float(x) for x in th), tuple(float(x) for x in fr),
                   d["embargo_bars"])

    @classmethod
    def from_file(cls, path) -> "ResearchConfig":
        return cls.from_mapping(_load(path))

    def canonical(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def with_(self, **ch) -> "ResearchConfig":
        return ResearchConfig.from_mapping({**self.canonical(), **ch})

    @property
    def config_hash(self) -> str:
        return _hash(self.canonical())


@dataclass(frozen=True)
class CostModel:
    """Explicit, versioned execution-cost ASSUMPTIONS for OHLCV simulation.
    Market entries/stops/time exits pay taker fee + slippage; take-profit limits pay maker fee."""
    taker_fee_bps: float
    maker_fee_bps: float
    slippage_bps: float
    funding_mode: str
    assumed_funding_rate_8h: float

    @classmethod
    def from_mapping(cls, d: Mapping[str, Any]) -> "CostModel":
        names = {f.name for f in fields(cls)}
        if set(d) != names:
            raise DomainError(f"costs keys must be exactly {sorted(names)}")
        for k in ("taker_fee_bps", "maker_fee_bps", "slippage_bps"):
            _num(k, d[k], 0, 1000, lo_open=False)
        if d["funding_mode"] not in FUNDING_MODES:
            raise DomainError(f"funding_mode must be one of {FUNDING_MODES}")
        _num("assumed_funding_rate_8h", d["assumed_funding_rate_8h"], -1, 1)
        return cls(float(d["taker_fee_bps"]), float(d["maker_fee_bps"]), float(d["slippage_bps"]), d["funding_mode"],
                   float(d["assumed_funding_rate_8h"]))

    def canonical(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @property
    def is_zero_cost(self) -> bool:
        return self.taker_fee_bps == 0 and self.maker_fee_bps == 0 and self.slippage_bps == 0 \
            and self.funding_mode == "none"


@dataclass(frozen=True)
class BacktestConfig:
    schema: int
    rule_name: str
    setup_types: tuple[SetupType, ...]
    entry_on: str                          # candidate | confirmed : the lifecycle fact that triggers the rule
    regime_fit_allowed: tuple[RegimeFit, ...]
    stop_buffer_atr: float                 # stop = invalidation reference -/+ buffer * ATR(at setup)
    take_profit_r: float
    max_hold_bars: int
    risk_per_trade_quote: float            # position size = risk / |reference price - stop|
    initial_equity_quote: float
    costs: CostModel

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "BacktestConfig":
        d = _keys(cls, raw)
        try:
            types = tuple(SetupType(t) for t in d["setup_types"])
            fits = tuple(RegimeFit(x) for x in d["regime_fit_allowed"])
        except ValueError as e:
            raise DomainError(f"backtest config: {e}") from None
        if not types or not fits:
            raise DomainError("setup_types and regime_fit_allowed must be non-empty")
        if d["entry_on"] not in ANCHORS:
            raise DomainError(f"entry_on must be one of {ANCHORS}")
        if not isinstance(d["rule_name"], str) or not d["rule_name"]:
            raise DomainError("rule_name required")
        _num("stop_buffer_atr", d["stop_buffer_atr"], 0, 10, lo_open=False)
        _num("take_profit_r", d["take_profit_r"], 0, 100)
        _num("max_hold_bars", d["max_hold_bars"], 0, 100_000, integer=True)
        _num("risk_per_trade_quote", d["risk_per_trade_quote"], 0, 1e12)
        _num("initial_equity_quote", d["initial_equity_quote"], 0, 1e15)
        return cls(1, d["rule_name"], types, d["entry_on"], fits, float(d["stop_buffer_atr"]),
                   float(d["take_profit_r"]), d["max_hold_bars"], float(d["risk_per_trade_quote"]),
                   float(d["initial_equity_quote"]), CostModel.from_mapping(d["costs"]))

    @classmethod
    def from_file(cls, path) -> "BacktestConfig":
        return cls.from_mapping(_load(path))

    def canonical(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        d["setup_types"] = [t.value for t in self.setup_types]
        d["regime_fit_allowed"] = [x.value for x in self.regime_fit_allowed]
        d["costs"] = self.costs.canonical()
        return d

    def with_(self, **ch) -> "BacktestConfig":
        return BacktestConfig.from_mapping({**self.canonical(), **ch})

    @property
    def config_hash(self) -> str:
        return _hash(self.canonical())



@dataclass(frozen=True)
class DecisionRunConfig:
    """Phase 9: execution-model parameters of a Strategy + Risk backtest (the decision
    itself comes from the gate). Same cost model and fill model as Phase 7."""
    schema: int
    initial_equity_quote: float
    costs: CostModel

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "DecisionRunConfig":
        d = _keys(cls, raw)
        _num("initial_equity_quote", d["initial_equity_quote"], 0, 1e15)
        return cls(1, float(d["initial_equity_quote"]), CostModel.from_mapping(d["costs"]))

    @classmethod
    def from_file(cls, path) -> "DecisionRunConfig":
        return cls.from_mapping(_load(path))

    def canonical(self) -> dict:
        return {"schema": 1, "initial_equity_quote": self.initial_equity_quote, "costs": self.costs.canonical()}

    @property
    def config_hash(self) -> str:
        return _hash(self.canonical())
