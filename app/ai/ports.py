from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol


class AIActionKind(str, Enum):
    NO_ACTION = "no_action"
    REDUCE_RISK = "reduce_risk"           # may be auto-applied
    DISABLE_STRATEGY = "disable_strategy" # may be auto-applied
    TIGHTEN_FILTER = "tighten_filter"     # may be auto-applied
    INCREASE_RISK = "increase_risk"       # requires manual confirmation
    LOOSEN_FILTER = "loosen_filter"       # requires manual confirmation


AUTO_APPLICABLE = {AIActionKind.NO_ACTION, AIActionKind.REDUCE_RISK,
                   AIActionKind.DISABLE_STRATEGY, AIActionKind.TIGHTEN_FILTER}


@dataclass(frozen=True)
class AIContext:
    purpose: str                        # "strategy_audit", "trade_review", ...
    payload: Mapping[str, Any]          # prepared, serialisable, secret-free data
    schema_version: str = "1"


@dataclass(frozen=True)
class AIDecision:
    kind: AIActionKind
    rationale: str
    confidence: float
    params: Mapping[str, Any] = field(default_factory=dict)
    provider: str = ""
    model: str = ""

    @property
    def requires_human(self) -> bool:
        return self.kind not in AUTO_APPLICABLE


class LLMClient(Protocol):
    provider: str
    model: str

    def decide(self, context: AIContext) -> AIDecision: ...
