"""Registry of analytics data sources (TZ Phase 8, section 3). A source that QRE does
not record is `source_not_available`; nothing is ever synthesised for it."""
from __future__ import annotations

from dataclasses import dataclass

from app.research.lab import stats

AVAILABLE = "available"
SCHEMA_ONLY = "schema_only_no_data"        # tables exist, nothing in QRE writes them, no adapter exists
NOT_AVAILABLE = "source_not_available"


@dataclass(frozen=True)
class SourceInfo:
    source_id: str
    tables: tuple[str, ...]
    status: str
    provides: str


REGISTRY: tuple[SourceInfo, ...] = (
    SourceInfo("research.trades", ("sim_trades", "research_runs", "research_datasets"), AVAILABLE,
               "hypothetical-rule trades, costs, R, exit reasons"),
    SourceInfo("research.equity", ("sim_equity",), AVAILABLE, "MTM equity per bar -> drawdown, curves"),
    SourceInfo("research.observations", ("replay_observations",), AVAILABLE,
               "setups as seen by the replay, first_regime_fit"),
    SourceInfo("research.outcomes", ("outcome_observations",), AVAILABLE, "setup MFE/MAE/forward return by horizon"),
    SourceInfo("research.runs", ("research_runs", "metric_reports"), AVAILABLE,
               "skipped signals, cost model, versions, warnings"),
    SourceInfo("market.candles", ("candles",), AVAILABLE, "trade MAE/MFE, capture, efficiency (computed)"),
    SourceInfo("live.setups", ("setups", "setup_events", "setup_snapshots"), AVAILABLE, "live setup lifecycle funnel"),
    SourceInfo("live.regime", ("regime_snapshots",), AVAILABLE, "live regime distribution, BTC context"),
    SourceInfo("live.structure", ("structure_snapshots", "structure_events"), AVAILABLE,
               "BOS/CHoCH/sweep frequency, directions"),
    SourceInfo("live.monitor", ("monitor_runs", "monitor_events"), AVAILABLE, "runtime health, unit statuses"),
    SourceInfo("integrity.conflicts", ("setup_conflicts", "structure_event_conflicts", "research_conflicts"),
               AVAILABLE, "anomalies"),
    SourceInfo("real.trades", ("trades", "fills", "trade_legs"), SCHEMA_ONLY,
               "real trades: no writer and no adapter in QRE"),
    SourceInfo("ai.decisions", (), NOT_AVAILABLE, "no AI/LLM layer in QRE"),
    SourceInfo("risk.decisions", (), NOT_AVAILABLE, "no Risk Engine in QRE"),
    SourceInfo("market.quotes", (), NOT_AVAILABLE, "bid/ask are not recorded"),
    SourceInfo("exec.post_exit", (), NOT_AVAILABLE, "no post-exit tracking of real trades"),
)
_BY_ID = {s.source_id: s for s in REGISTRY}


def get(source_id: str) -> SourceInfo:
    try:
        return _BY_ID[source_id]
    except KeyError:
        raise LookupError(f"unknown analytics source {source_id!r}") from None


def require(source_id: str) -> dict | None:
    """None when usable, else the standard not_available metric with reason source_not_available."""
    return None if get(source_id).status == AVAILABLE else stats.na(NOT_AVAILABLE)


def availability(table_names: set[str]) -> list[dict]:
    """Status of every source; an 'available' source whose tables are missing is reported as such."""
    out = []
    for s in REGISTRY:
        status = s.status
        if status == AVAILABLE and not set(s.tables) <= table_names:
            status = f"{NOT_AVAILABLE}:missing_tables"
        out.append({"source": s.source_id, "status": status, "tables": list(s.tables), "provides": s.provides})
    return out
