"""StrategyRiskGate: Strategy -> Risk Engine, implementing research.lab.ports.DecisionGate
structurally (research never imports strategy/risk). Wired in the CLI. No execution."""
from __future__ import annotations

from decimal import Decimal
from typing import Mapping

from app.domain.decision import GateResult, InstrumentRules, PortfolioState, SetupView
from app.domain.enums import RiskStatus
from app.risk.engine import RiskEngineV1
from app.risk.version import rules_hash as risk_rules_hash
from app.strategy.engine import StrategyV1
from app.strategy.version import rules_hash as strategy_rules_hash

PIPELINE_ID = "strategy_risk_v1"


class StrategyRiskGate:
    pipeline_id = PIPELINE_ID

    def __init__(self, strategy: StrategyV1, risk: RiskEngineV1,
                 instruments: Mapping[str, InstrumentRules]) -> None:
        self.strategy, self.risk = strategy, risk
        self.instruments = dict(instruments)
        self.entry_on = strategy.cfg.entry_on
        self.config = {
            "pipeline": PIPELINE_ID,
            "strategy": {"strategy_id": strategy.strategy_id, "version": strategy.version,
                         "rules_hash": strategy_rules_hash(), "config": strategy.cfg.canonical()},
            "risk": {"model_version": risk.model_version, "rules_hash": risk_rules_hash(),
                     "config": risk.cfg.canonical()},
            "instrument_rules": {s: r.to_dict() for s, r in sorted(self.instruments.items())},
        }

    def evaluate(self, view: SetupView, reference_price: Decimal, portfolio: PortfolioState,
               instrument: InstrumentRules | None) -> GateResult:
        s = self.strategy.evaluate(view)
        if not s.accepted:
            return GateResult(view.setup_id, s, None, False, s.reason_code)
        r = self.risk.assess(s, reference_price, portfolio, instrument)
        return GateResult(view.setup_id, s, r, r.status is RiskStatus.APPROVED, r.reason_code)
