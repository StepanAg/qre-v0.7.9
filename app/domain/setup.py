"""Setup detection model (Phase 6).

A Setup describes an OBSERVED market situation with its evidence, contradictions,
lifecycle and invalidation rule. It is NOT a trading signal and grants no
permission to open a position. Four separate notions are kept apart:
  status        - lifecycle (candidate / confirmed / invalidated / expired)
  data_quality  - were the inputs usable
  regime_fit    - does the as_of regime match the type's allowed regimes
  (tradeability - does not exist here; decided by later phases, if ever)
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from app.domain.common import req, utc
from app.domain.enums import (Category, QualityStatus, RegimeFit, SetupStatus, SetupType, StructureDirection)
from app.domain.errors import DomainError
from app.domain.market import Symbol, Timeframe

_T = datetime.fromisoformat
TERMINAL = {SetupStatus.INVALIDATED, SetupStatus.EXPIRED}
_ALLOWED_NEXT = {
    SetupStatus.CANDIDATE: {SetupStatus.CONFIRMED, SetupStatus.INVALIDATED, SetupStatus.EXPIRED},
    SetupStatus.CONFIRMED: {SetupStatus.INVALIDATED, SetupStatus.EXPIRED},
    SetupStatus.INVALIDATED: set(), SetupStatus.EXPIRED: set(),
}


@dataclass(frozen=True)
class SetupTransition:
    status: SetupStatus
    at: datetime          # close time of the bar that caused the transition
    reason: str

    def to_dict(self) -> dict:
        return {"status": self.status.value, "at": self.at.isoformat(), "reason": self.reason}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "SetupTransition":
        return cls(SetupStatus(d["status"]), _T(d["at"]), d["reason"])


@dataclass(frozen=True)
class Setup:
    setup_id: str
    symbol: str
    timeframe: Timeframe
    setup_type: SetupType
    direction: StructureDirection
    trigger_event_id: str
    trigger_time: datetime               # when the triggering fact became known (bar close)
    key_level: float
    invalidation_condition: str          # human-readable rule, e.g. "close < 108.0"
    transitions: tuple[SetupTransition, ...]
    reason_codes: tuple[str, ...]
    evidence: tuple[tuple[str, Any], ...]      # facts FOR the setup (sorted key/value pairs)
    contradictions: tuple[str, ...]            # facts AGAINST it at as_of
    regime_fit: RegimeFit
    data_quality: QualityStatus
    engine_version: str
    config_hash: str

    def __post_init__(self) -> None:
        req("setup_id", self.setup_id)
        utc("trigger_time", self.trigger_time)
        if self.direction is StructureDirection.UNKNOWN:
            raise DomainError("a setup needs a direction; unknown direction -> no setup")
        if self.data_quality is not QualityStatus.VALID:
            raise DomainError("setups exist only on VALID data")
        tr = self.transitions
        if not tr or tr[0].status is not SetupStatus.CANDIDATE:
            raise DomainError("lifecycle must start with CANDIDATE")
        if tr[0].at < self.trigger_time:
            raise DomainError("a setup cannot be known before its trigger")
        for a, b in zip(tr, tr[1:]):
            if b.status not in _ALLOWED_NEXT[a.status]:
                raise DomainError(f"illegal transition {a.status.value} -> {b.status.value}")
            if b.at <= a.at:
                raise DomainError("transitions must be strictly ordered in time")
        if list(self.evidence) != sorted(self.evidence, key=lambda kv: kv[0]):
            raise DomainError("evidence must be sorted by key")

    @property
    def status(self) -> SetupStatus:
        return self.transitions[-1].status

    @property
    def setup_time(self) -> datetime:
        return self.transitions[0].at

    @property
    def confirmed_at(self) -> datetime | None:
        return next((t.at for t in self.transitions if t.status is SetupStatus.CONFIRMED), None)

    @property
    def closed_at(self) -> datetime | None:
        return self.transitions[-1].at if self.status in TERMINAL else None

    @property
    def is_active(self) -> bool:
        return self.status not in TERMINAL

    def identity(self) -> dict:
        """What the setup IS (never changes once created)."""
        return {"setup_id": self.setup_id, "symbol": self.symbol, "timeframe": self.timeframe.value,
                "setup_type": self.setup_type.value, "direction": self.direction.value,
                "trigger_event_id": self.trigger_event_id, "trigger_time": self.trigger_time.isoformat(),
                "setup_time": self.setup_time.isoformat(), "key_level": self.key_level,
                "invalidation_condition": self.invalidation_condition, "engine_version": self.engine_version,
                "config_hash": self.config_hash}

    def to_dict(self) -> dict:
        return {**self.identity(), "status": self.status.value,
                "confirmed_at": self.confirmed_at.isoformat() if self.confirmed_at else None,
                "closed_at": self.closed_at.isoformat() if self.closed_at else None,
                "transitions": [t.to_dict() for t in self.transitions], "reason_codes": list(self.reason_codes),
                "evidence": [[k, v] for k, v in self.evidence], "contradictions": list(self.contradictions),
                "regime_fit": self.regime_fit.value, "data_quality": self.data_quality.value}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Setup":
        return cls(d["setup_id"], d["symbol"], Timeframe(d["timeframe"]), SetupType(d["setup_type"]),
                   StructureDirection(d["direction"]), d["trigger_event_id"], _T(d["trigger_time"]), d["key_level"],
                   d["invalidation_condition"], tuple(SetupTransition.from_dict(t) for t in d["transitions"]),
                   tuple(d["reason_codes"]), tuple((k, v) for k, v in d["evidence"]), tuple(d["contradictions"]),
                   RegimeFit(d["regime_fit"]), QualityStatus(d["data_quality"]), d["engine_version"],
                   d["config_hash"])


@dataclass(frozen=True)
class SetupSnapshot:
    symbol: Symbol
    timeframe: Timeframe
    as_of: datetime                       # data horizon: nothing after this is used
    evaluated_at: datetime                # wall-clock time of the evaluation (not part of the result)
    data_quality: QualityStatus
    reason_codes: tuple[str, ...]
    setups: tuple[Setup, ...]             # active + recently closed, ordered by setup_time
    regime_context: Mapping[str, Any] | None      # RegimeSnapshot reference at as_of
    structure_context: Mapping[str, Any] | None   # StructureSnapshot reference at as_of
    code_version: str
    engine_version: str
    config_hash: str
    input_fingerprint: str

    def __post_init__(self) -> None:
        utc("as_of", self.as_of)
        utc("evaluated_at", self.evaluated_at)
        for n in ("code_version", "engine_version", "config_hash", "input_fingerprint"):
            req(n, getattr(self, n))
        if not self.reason_codes:
            raise DomainError("a setup snapshot needs reason codes")
        if self.data_quality is not QualityStatus.VALID and self.setups:
            raise DomainError(f"no setups may be reported on {self.data_quality.value} data")
        for s in self.setups:
            if s.transitions[-1].at > self.as_of or s.trigger_time > self.as_of:
                raise DomainError("setup facts after as_of (look-ahead)")
            if s.symbol != self.symbol.name or s.timeframe is not self.timeframe:
                raise DomainError("setup belongs to another series")
        if len({s.setup_id for s in self.setups}) != len(self.setups):
            raise DomainError("duplicate setup ids in one snapshot")

    @property
    def key(self) -> tuple:
        return (self.symbol.category.value, self.symbol.name, self.timeframe.value, self.as_of,
                self.engine_version, self.config_hash, self.input_fingerprint)

    @property
    def active(self) -> tuple[Setup, ...]:
        return tuple(s for s in self.setups if s.is_active)

    def to_dict(self, *, include_evaluated_at: bool = True) -> dict:
        d = {"symbol": self.symbol.name, "category": self.symbol.category.value,
             "timeframe": self.timeframe.value, "as_of": self.as_of.isoformat(),
             "data_quality": self.data_quality.value, "reason_codes": list(self.reason_codes),
             "setups": [s.to_dict() for s in self.setups],
             "regime_context": dict(self.regime_context) if self.regime_context else None,
             "structure_context": dict(self.structure_context) if self.structure_context else None,
             "code_version": self.code_version, "engine_version": self.engine_version,
             "config_hash": self.config_hash, "input_fingerprint": self.input_fingerprint}
        if include_evaluated_at:
            d["evaluated_at"] = self.evaluated_at.isoformat()
        return d

    def canonical_json(self) -> str:
        """Deterministic result (evaluated_at excluded): the idempotency payload."""
        return json.dumps(self.to_dict(include_evaluated_at=False), sort_keys=True, separators=(",", ":"),
                          allow_nan=False)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], evaluated_at: datetime | None = None) -> "SetupSnapshot":
        ev = evaluated_at or (_T(d["evaluated_at"]) if d.get("evaluated_at") else _T(d["as_of"]))
        return cls(Symbol(d["symbol"], Category(d["category"])), Timeframe(d["timeframe"]), _T(d["as_of"]), ev,
                   QualityStatus(d["data_quality"]), tuple(d["reason_codes"]),
                   tuple(Setup.from_dict(s) for s in d["setups"]), d["regime_context"], d["structure_context"],
                   d["code_version"], d["engine_version"], d["config_hash"], d["input_fingerprint"])
