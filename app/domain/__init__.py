"""Pure domain model. Imports only the standard library and app.domain.

Decision chain (Phase 9, canonical - entities are never merged):
setup.Setup (Phase 6) -> SetupView (point-in-time) -> StrategyDecision -> RiskDecision (+ RiskPlan)
  -> Order -> Fill -> PositionEvent -> Trade          (Order onwards: execution, not implemented)

Deprecated, kept physically (decision D9-1): decision.Signal, decision.Setup, research.ResearchSnapshot.
"""
