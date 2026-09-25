"""Strategy port (Phase 9). Input: a point-in-time SetupView of the canonical Phase 6
setup. Output: a StrategyDecision. A strategy never sizes, never reads prices, balances
or instrument rules, and never executes. (The Phase 0 port on ResearchSnapshot /
decision.Setup / Signal is retired - those types are deprecated, decision D9-1.)"""
from __future__ import annotations

from typing import Protocol

from app.domain.decision import SetupView, StrategyDecision


class Strategy(Protocol):
    strategy_id: str
    version: str
    config_hash: str

    def evaluate(self, view: SetupView) -> StrategyDecision: ...
