"""Phase 8 fixtures: research runs written through the REAL research store
(SQLiteResearchStore.save_run) with hand-built simulated trades - no replay needed.
All numbers are chosen so expected metrics can be computed by hand."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.domain.enums import SimExitReason
from app.domain.research_lab import ResearchDataset, SeriesCoverage, SimulatedTrade
from app.research.lab.versions import component_versions
from app.storage.research import SQLiteResearchStore
from tests.fakes import TempDB

T0 = datetime(2026, 3, 2, tzinfo=timezone.utc)
M15 = timedelta(minutes=15)
RUN, REPLAY, OUTC, DS = "bt_fixture", "rp_fixture", "oc_fixture", "ds_fixture"


def trade(no: int, net: float, *, fees: float = 1.0, funding: float = 0.0, slippage: float = 0.0,
          risk: float = 100.0, symbol: str = "ETHUSDT", direction: str = "bullish", setup_type: str = "breakout",
          reason: SimExitReason = SimExitReason.TAKE_PROFIT, entry_bar: int | None = None, bars: int = 2,
          entry: float = 100.0, stop: float | None = None, setup_id: str | None = None, run_id: str = RUN,
          r: float | None | str = "auto") -> SimulatedTrade:
    """gross is derived so that net = gross - fees + funding holds exactly."""
    eb = no * 10 if entry_bar is None else entry_bar
    entry_t = T0 + M15 * eb
    exit_t = entry_t + M15 * bars
    long = direction == "bullish"
    stop = stop if stop is not None else (entry - 1.0 if long else entry + 1.0)
    qty = (risk or 100.0) / abs(entry - stop)          # zero-risk trades keep a real size
    gross = net + fees - funding
    exit_price = entry + (gross / qty if long else -gross / qty)
    rr = (net / risk if risk else None) if r == "auto" else r
    return SimulatedTrade(run_id, no, DS, symbol, setup_id or f"su_{no}", setup_type, direction, entry_t, entry_t,
                          entry, qty, stop, entry + (2 if long else -2), exit_t, exit_price, reason, bars, gross, fees,
                          funding, slippage, net, risk, rr, "none", reason is SimExitReason.STOP_AMBIGUOUS)


def dataset(timeframe: str = "15m", symbols=("ETHUSDT",), versions=None) -> dict:
    return ResearchDataset(DS, {"symbols": list(symbols), "timeframe": timeframe,
                                "start": T0.isoformat(), "end": (T0 + M15 * 400).isoformat(), "with_context": False},
                           (SeriesCoverage(symbols[0], timeframe, "primary", 400, "fp", (), 0),), {},
                           versions or component_versions(), {"setup": "h"}, {"primary_candles": 400}).to_dict()


class AnalyticsLab:
    """Temp DB with one dataset, one replay run and one backtest run holding `trades`."""

    def __init__(self, trades=(), *, equity=None, observations=(), outcomes=(), summary=None, config=None,
                 versions=None, run_id: str = RUN) -> None:
        self.db = TempDB()
        self.store = SQLiteResearchStore(self.db.writer)
        v = versions or component_versions()
        self.store.save_dataset(dataset(versions=v))
        self.store.save_run(run_id=REPLAY, kind="replay", parent_id=DS, dataset_id=DS, config={"with_regime": False},
                            versions=v, summary={"observations": len(observations)},
                            full_result={"obs": [o["setup"]["setup_id"] for o in observations]},
                            observations=list(observations))
        if outcomes:
            self.store.save_run(run_id=OUTC, kind="outcomes", parent_id=REPLAY, dataset_id=DS, config={},
                                versions=v, summary={}, full_result={"n": len(outcomes)},
                                outcomes=[o.to_dict() for o in outcomes])
        self.add_backtest(run_id, trades, equity=equity, summary=summary, config=config, versions=v)

    def add_backtest(self, run_id, trades, *, equity=None, summary=None, config=None, versions=None) -> None:
        cfg = {"rule_name": "fixture_rule", "initial_equity_quote": 10000.0, "config_hash_hint": run_id,
               **(config or {})}
        eq = equity if equity is not None else []
        self.store.save_run(run_id=run_id, kind="backtest", parent_id=REPLAY, dataset_id=DS, config=cfg,
                            versions=versions or component_versions(),
                            summary=summary or {"rule": "fixture_rule", "skipped_signals": {}},
                            full_result={"t": [t.to_dict() for t in trades], "e": [(a.isoformat(), b) for a, b in eq]},
                            trades=[t.to_dict() for t in trades], equity=eq)

    @property
    def path(self):
        return self.db.path

    def reader(self):
        from app.storage.analytics_read import SQLiteAnalyticsReader
        return SQLiteAnalyticsReader(self.db.path)

    def close(self) -> None:
        self.db.close()
