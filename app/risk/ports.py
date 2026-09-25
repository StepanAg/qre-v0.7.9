"""Risk Engine port (Phase 9). Input: an accepted StrategyDecision, the decision price,
the portfolio state and instrument rules. Output: a RiskDecision (APPROVED with an
unchanged Phase 0 RiskPlan, REJECTED or HALTED). Deterministic Python only: no LLM can
change risk budget, leverage, stop, target, entry or size. Never executes."""
from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from app.domain.decision import InstrumentRules, PortfolioState, RiskDecision, StrategyDecision


class RiskEngine(Protocol):
    model_version: str
    config_hash: str

    def assess(self, decision: StrategyDecision, reference_price: Decimal, portfolio: PortfolioState,
               instrument: InstrumentRules | None) -> RiskDecision: ...
