"""Ports of the research lab (Phase 9). research must not import strategy/risk, so the
backtest receives the decision pipeline through this Protocol (implemented by
app.strategy.pipeline.StrategyRiskGate, wired in the CLI)."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping, Protocol

from app.domain.decision import GateResult, InstrumentRules, PortfolioState, SetupView


class DecisionGate(Protocol):
    pipeline_id: str
    entry_on: str                           # candidate | confirmed: when a decision can be taken
    config: Mapping[str, Any]               # full snapshot (strategy, risk, instrument rules) -> run id

    def evaluate(self, view: SetupView, reference_price: Decimal, portfolio: PortfolioState,
               instrument: InstrumentRules | None) -> GateResult: ...
