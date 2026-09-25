"""Research & backtest records (Phase 7).

Separate entity families, never mixed:
  ResearchDataset   - WHICH historical data (candles referenced by fingerprint)
  ReplayObservation - WHAT the analytics knew, bar by bar (setups as observed)
  OutcomeObservation- what price did AFTER a setup (explicit future-looking layer)
  SimulatedTrade    - a HYPOTHETICAL trade of a test rule (never a real trade)
All carry the run/dataset ids they came from."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Mapping

from app.domain.enums import OutcomeStatus, SimExitReason
from app.domain.errors import DomainError


def canonical(d: Any) -> str:
    return json.dumps(d, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


@dataclass(frozen=True)
class Segment:
    start: datetime           # open time of first bar
    end: datetime             # exclusive: close time of the last bar
    bars: int
    usable: bool
    reason: str               # "ok" or why it is excluded

    def to_dict(self) -> dict:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(), "bars": self.bars,
                "usable": self.usable, "reason": self.reason}


@dataclass(frozen=True)
class SeriesCoverage:
    symbol: str
    timeframe: str
    role: str                 # "primary" | "context" (regime timeframes / BTC)
    candles: int
    fingerprint: str          # hash of every OHLCV value in range: data changes -> new dataset
    segments: tuple[Segment, ...]
    missing_bars: int

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "timeframe": self.timeframe, "role": self.role, "candles": self.candles,
                "fingerprint": self.fingerprint, "segments": [s.to_dict() for s in self.segments],
                "missing_bars": self.missing_bars}


@dataclass(frozen=True)
class ResearchDataset:
    dataset_id: str                     # hash(spec + versions + configs + data fingerprints)
    spec: Mapping[str, Any]             # symbols, timeframe, start, end, with_context
    coverages: tuple[SeriesCoverage, ...]
    funding: Mapping[str, Any]          # per symbol: count + fingerprint of funding rates in range
    versions: Mapping[str, str]         # code, feature catalog, regime/structure/setup rules
    config_hashes: Mapping[str, str]
    quality_summary: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.coverages:
            raise DomainError("a dataset needs at least one series")

    def to_dict(self) -> dict:
        return {"dataset_id": self.dataset_id, "spec": dict(self.spec),
                "coverages": [c.to_dict() for c in self.coverages], "funding": dict(self.funding),
                "versions": dict(self.versions), "config_hashes": dict(self.config_hashes),
                "quality_summary": dict(self.quality_summary)}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ResearchDataset":
        T = datetime.fromisoformat
        covs = tuple(SeriesCoverage(c["symbol"], c["timeframe"], c["role"], c["candles"], c["fingerprint"],
                                    tuple(Segment(T(s["start"]), T(s["end"]), s["bars"], s["usable"], s["reason"])
                                          for s in c["segments"]), c["missing_bars"]) for c in d["coverages"])
        return cls(d["dataset_id"], d["spec"], covs, d["funding"], d["versions"], d["config_hashes"],
                   d["quality_summary"])

    def primary(self, symbol: str) -> SeriesCoverage:
        return next(c for c in self.coverages if c.symbol == symbol and c.role == "primary")


@dataclass(frozen=True)
class OutcomeObservation:
    setup_id: str
    symbol: str
    setup_type: str
    direction: str
    anchor: str                       # "candidate" | "confirmed"
    anchor_time: datetime             # close of the anchor bar (what was known then)
    horizon: int                      # bars after the anchor
    status: OutcomeStatus
    reason: str
    values: Mapping[str, float | int | bool | str | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status is not OutcomeStatus.VALID and self.values:
            raise DomainError("censored / incomplete observations carry no outcome values (never zeros)")

    def to_dict(self) -> dict:
        return {"setup_id": self.setup_id, "symbol": self.symbol, "setup_type": self.setup_type,
                "direction": self.direction, "anchor": self.anchor, "anchor_time": self.anchor_time.isoformat(),
                "horizon": self.horizon, "status": self.status.value, "reason": self.reason,
                "values": dict(sorted(self.values.items()))}


@dataclass(frozen=True)
class SimulatedTrade:
    """Hypothetical trade of a TEST RULE in an OHLCV simulation. Never a real trade;
    stored apart from the real `trades` table."""
    run_id: str
    trade_no: int
    dataset_id: str
    symbol: str
    setup_id: str
    setup_type: str
    direction: str
    decision_time: datetime           # when the rule could decide (setup fact known)
    entry_time: datetime              # open of the next bar: never the decision bar's own close
    entry_price: float
    qty: float
    stop: float
    take_profit: float
    exit_time: datetime
    exit_price: float
    exit_reason: SimExitReason
    bars_held: int
    gross_pnl: float                  # at actual (slipped) fill prices
    fees: float
    funding: float                    # signed: + received / - paid
    slippage_cost: float              # attribution only: already inside gross_pnl
    net_pnl: float                    # gross - fees + funding
    initial_risk: float
    r_multiple: float | None
    funding_source: str               # data | assumed | none
    ambiguous: bool

    def __post_init__(self) -> None:
        if self.entry_time < self.decision_time:
            raise DomainError("a simulated fill cannot precede the decision")
        if abs((self.gross_pnl - self.fees + self.funding) - self.net_pnl) > 1e-6 * max(1.0, abs(self.net_pnl)):
            raise DomainError("net must equal gross - fees + funding (no double counting)")

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("decision_time", "entry_time", "exit_time"):
            d[k] = d[k].isoformat()
        d["exit_reason"] = self.exit_reason.value
        return d
