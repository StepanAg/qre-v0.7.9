"""Component registry: the single source of truth for what is implemented.

The test runner reads this to report NOT_IMPLEMENTED honestly. A component is
only reported as implemented when a concrete class is registered AND a test
module covers it; an empty module or a bare interface never counts.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ComponentInfo:
    name: str
    phase: int                  # phase in which it is planned
    implementation: str | None  # "package.module:Class" or None
    test_module: str | None     # tests module covering it


COMPONENTS: tuple[ComponentInfo, ...] = (
    ComponentInfo("Market Data", 1, "app.data.bybit.provider:BybitMarketDataProvider",
                  "tests.test_md_backfill"),
    ComponentInfo("Research / Features", 2, "app.research.engine:FeatureEngine",
                  "tests.test_features_calc"),
    ComponentInfo("Regime Engine", 3, "app.research.regime.engine:RegimeEngine",
                  "tests.test_regime_rules"),
    ComponentInfo("Market Monitor", 4, "app.monitor.runtime:MarketMonitor", "tests.test_monitor"),
    ComponentInfo("Market Structure", 5, "app.research.structure.engine:StructureEngine", "tests.test_structure"),
    ComponentInfo("Analytics", 8, "app.analytics.engine:AnalyticsEngine", "tests.test_analytics.AnalyticsSmokeTests"),
    ComponentInfo("Setup Detection", 6, "app.research.setup.engine:SetupEngine", "tests.test_setup"),
    ComponentInfo("Strategy", 9, "app.strategy.engine:StrategyV1", "tests.test_phase9.StrategyTests"),
    ComponentInfo("Risk Engine", 9, "app.risk.engine:RiskEngineV1", "tests.test_phase9.RiskEngineTests"),
    ComponentInfo("Backtest", 7, "app.research.lab.backtest:BacktestEngine", "tests.test_research.BacktestTests"),
    ComponentInfo("Execution", 10, None, None),
    ComponentInfo("Reconciliation", 10, None, None),
    ComponentInfo("AI", 10, None, None),
    ComponentInfo("Telegram", 10, None, None),
)


def get(name: str) -> ComponentInfo:
    for c in COMPONENTS:
        if c.name == name:
            return c
    raise KeyError(name)
