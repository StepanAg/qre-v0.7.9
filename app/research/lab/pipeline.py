"""Research pipeline: dataset -> replay -> outcomes / backtest -> chronological splits.
Orchestration only (the analytics are the existing services). Runs offline on stored
data, never inside the monitor loop, and persists each result once, immutably."""
from __future__ import annotations

from datetime import datetime
from typing import Sequence

from app.data.ports import MarketDataStore
from app.domain.errors import DomainError
from app.domain.market import Symbol, Timeframe
from app.domain.research_lab import OutcomeObservation, ResearchDataset
from app.research.lab import outcomes as oc
from app.research.lab import splits
from app.research.lab.backtest import BacktestEngine, BacktestResult, DecisionBacktestEngine
from app.research.lab.config import DecisionRunConfig
from app.research.lab.config import BacktestConfig, ResearchConfig
from app.research.lab.dataset import DatasetBuilder
from app.research.lab.metrics import report as bt_report
from app.research.lab.metrics import trade_metrics
from app.research.lab.replay import ReplayEngine, ReplayObservation, ReplayResult
from app.research.ports import ResearchStore
from app.research.regime.service import RegimeService
from app.research.setup.service import SetupService

LIMITATIONS = [
    "OHLCV simulation: no order book, no queue position, no market impact; fills are modelled, not observed",
    "intra-bar order of high/low is unknown: stop assumed first when stop and target share a bar",
    "fees/slippage/funding are configured ASSUMPTIONS unless funding data is present in the dataset",
    "results are in-sample unless read from the chronological test split; no parameter was optimised here",
    "a hypothetical test rule is not a strategy and not a trading recommendation",
]


class ResearchPipeline:
    def __init__(self, market: MarketDataStore, setups: SetupService, cfg: ResearchConfig, store: ResearchStore,
                 regime: RegimeService | None = None) -> None:
        self.market, self.setups, self.cfg, self.store, self.regime = market, setups, cfg, store, regime
        context: list[tuple[str, Timeframe]] = []
        if regime is not None:
            context = [("*", t) for t in regime.config.timeframes] + [(regime.config.btc_symbol.name,
                                                                        regime.config.btc_timeframe)]
        hashes = {"setup": setups.config.config_hash, "structure": setups.structure.config.config_hash,
                  "research": cfg.config_hash}
        if regime is not None:
            hashes["regime"] = regime.config.config_hash
        self.builder = DatasetBuilder(market, min_segment_bars=setups.engine.required_bars(), config_hashes=hashes,
                                      context_series=context)

    # ---------------------------------------------------------------- dataset
    def build_dataset(self, symbols: Sequence[str], tf: Timeframe, start: datetime, end: datetime, *,
                      with_context: bool = False) -> tuple[ResearchDataset, bool]:
        if with_context and self.regime is None:
            raise DomainError("with_context needs a regime service")
        ds = self.builder.build(symbols, tf, start, end, with_context=with_context)
        return ds, self.store.save_dataset(ds.to_dict())

    def dataset(self, dataset_id: str, *, verify: bool = True) -> ResearchDataset:
        d = self.store.dataset(dataset_id)
        if d is None:
            raise LookupError(f"dataset {dataset_id} not found")
        ds = ResearchDataset.from_dict(d)
        if verify:
            self.builder.verify(ds)
        return ds

    def _candles(self, ds: ResearchDataset) -> dict:
        return {s: self.builder.candles(ds, s) for s in ds.spec["symbols"]}

    # ----------------------------------------------------------------- replay
    def replay(self, dataset_id: str, *, with_regime: bool = False) -> tuple[ReplayResult, bool]:
        ds = self.dataset(dataset_id)
        if with_regime and not ds.spec.get("with_context"):
            raise DomainError("replay with regime needs a dataset built with context series (--with-context)")
        engine = ReplayEngine(self.setups, self.regime if with_regime else None)
        res = engine.run(ds, self._candles(ds))
        obs = [o.to_dict() for o in res.observations]
        created = self.store.save_run(
            run_id=res.run_id, kind="replay", parent_id=ds.dataset_id, dataset_id=ds.dataset_id,
            config={"with_regime": with_regime}, versions=dict(ds.versions), summary=res.summary(),
            full_result={"summary": res.summary(), "observations": obs}, observations=obs)
        return res, created

    def load_replay(self, run_id: str) -> tuple[ReplayResult, ResearchDataset]:
        r = self.store.run(run_id)
        if r is None or r["kind"] != "replay":
            raise LookupError(f"replay run {run_id} not found")
        ds = self.dataset(r["dataset_id"])
        obs = [ReplayObservation.from_dict(o) for o in self.store.observations(run_id)]
        s = r["summary"]
        return ReplayResult(run_id, ds.dataset_id, s["with_regime"], obs, {}, s["replay_hash"]), ds

    # --------------------------------------------------------------- outcomes
    def outcomes(self, replay_run_id: str) -> tuple[str, list[OutcomeObservation], dict, bool]:
        rp, ds = self.load_replay(replay_run_id)
        tf = Timeframe(ds.spec["timeframe"])
        obs, extra = oc.analyze(rp, self._candles(ds), tf, self.cfg)
        summary = {"horizons": oc.summarize(obs, self.cfg), **extra, "anchor": self.cfg.outcome_anchor,
                   "note": "outcome analysis of setups - not trades, not a backtest"}
        split = self._split_outcomes(ds, obs)
        run_id = oc.outcome_run_id(replay_run_id, self.cfg)
        rows = [o.to_dict() for o in obs]
        created = self.store.save_run(
            run_id=run_id, kind="outcomes", parent_id=replay_run_id, dataset_id=ds.dataset_id,
            config=self.cfg.canonical(), versions=dict(ds.versions), summary=summary,
            full_result={"summary": summary, "rows": rows, "splits": split}, outcomes=rows,
            reports=[("outcomes", summary), ("splits", split)])
        return run_id, obs, summary, created

    def _split_outcomes(self, ds: ResearchDataset, obs: list[OutcomeObservation]) -> dict:
        tf = Timeframe(ds.spec["timeframe"])
        p = splits.plan(datetime.fromisoformat(ds.spec["start"]), datetime.fromisoformat(ds.spec["end"]),
                        self.cfg.split_fractions, tf, self.cfg.embargo_bars)
        out = {"plan": p.to_dict()}
        for h in self.cfg.outcome_horizons:
            rows = [o for o in obs if o.horizon == h]
            a = splits.assign(rows, p, lambda o: o.anchor_time, lambda o, h=h: o.anchor_time + tf.delta * h)
            out[str(h)] = {n: {"n": len(v), "summary": oc.summarize(v, self.cfg.with_(outcome_horizons=[h]))[str(h)]}
                           for n, v in a.split.items()} | {"purged": a.purged, "embargoed": a.embargoed}
        return out

    # --------------------------------------------------------------- backtest
    def backtest(self, replay_run_id: str, bt: BacktestConfig) -> tuple[BacktestResult, dict, bool]:
        rp, ds = self.load_replay(replay_run_id)
        tf = Timeframe(ds.spec["timeframe"])
        start, end = datetime.fromisoformat(ds.spec["start"]), datetime.fromisoformat(ds.spec["end"])
        funding = {s: self.market.funding_rates(Symbol(s), start, end) for s in ds.spec["symbols"]}
        res = BacktestEngine(bt).run(rp, self._candles(ds), tf, funding)
        metrics = bt_report(res)
        p = splits.plan(start, end, self.cfg.split_fractions, tf, self.cfg.embargo_bars)
        a = splits.assign(res.trades, p, lambda t: t.decision_time, lambda t: t.exit_time)
        split = {"plan": p.to_dict(), **{n: trade_metrics(v) for n, v in a.split.items()},
                 "purged": a.purged, "embargoed": a.embargoed}
        warnings = []
        if bt.costs.is_zero_cost:
            warnings.append("ZERO_COST_MODEL: results exclude all execution costs")
        if res.funding_sources.get("assumed"):
            warnings.append("FUNDING_ASSUMED: configured rate used where no funding data was in the dataset")
        summary = {"rule": bt.rule_name, "dataset_id": ds.dataset_id, "replay_run_id": replay_run_id,
                   "period": [ds.spec["start"], ds.spec["end"]], "symbols": ds.spec["symbols"],
                   "timeframe": ds.spec["timeframe"], "cost_model": bt.costs.canonical(),
                   "funding_sources": res.funding_sources, "skipped_signals": res.skipped,
                   "data_quality": ds.quality_summary, "warnings": warnings, "limitations": LIMITATIONS}
        trades = [t.to_dict() for t in res.trades]
        created = self.store.save_run(
            run_id=res.run_id, kind="backtest", parent_id=replay_run_id, dataset_id=ds.dataset_id,
            config=bt.canonical(), versions=dict(ds.versions), summary=summary,
            full_result={"summary": summary, "metrics": metrics, "trades": trades, "splits": split,
                         "equity": [(t.isoformat(), v) for t, v in res.equity]},
            trades=trades, equity=res.equity, reports=[("metrics", metrics), ("splits", split)])
        return res, {"summary": summary, "metrics": metrics, "splits": split}, created


    # ---------------------------------------------------- Phase 9: Strategy + Risk
    def decide(self, replay_run_id: str, gate, run_cfg: DecisionRunConfig) -> tuple:
        """Backtest where the DecisionGate (Strategy -> Risk) decides entry and size. Stored as a
        backtest run (no new kind, no migration); decisions go to metric_reports 'decisions'."""
        rp, ds = self.load_replay(replay_run_id)
        tf = Timeframe(ds.spec["timeframe"])
        start, end = datetime.fromisoformat(ds.spec["start"]), datetime.fromisoformat(ds.spec["end"])
        funding = {s: self.market.funding_rates(Symbol(s), start, end) for s in ds.spec["symbols"]}
        res = DecisionBacktestEngine(run_cfg).run_gate(rp, self._candles(ds), tf, funding, gate,
                                                       gate.instruments, run_cfg.config_hash)
        metrics = bt_report(res)
        p = splits.plan(start, end, self.cfg.split_fractions, tf, self.cfg.embargo_bars)
        a = splits.assign(res.trades, p, lambda t: t.decision_time, lambda t: t.exit_time)
        split = {"plan": p.to_dict(), **{n: trade_metrics(v) for n, v in a.split.items()},
                 "purged": a.purged, "embargoed": a.embargoed}
        warnings = []
        if run_cfg.costs.is_zero_cost:
            warnings.append("ZERO_COST_MODEL: results exclude all execution costs")
        if res.funding_sources.get("assumed"):
            warnings.append("FUNDING_ASSUMED: configured rate used where no funding data was in the dataset")
        missing = sorted(set(ds.spec["symbols"]) - set(gate.instruments))
        if missing and gate.config["risk"]["config"]["mode"] == "equity_pct":
            warnings.append(f"INSTRUMENT_RULES_MISSING: {missing} - every decision on them is rejected")
        if res.halted_at is not None:
            warnings.append(f"HALTED at {res.halted_at.isoformat()}: hard drawdown blocked all later entries "
                            "(open positions were not closed)")
        summary = {"rule": gate.pipeline_id, "pipeline": gate.pipeline_id, "dataset_id": ds.dataset_id,
                   "replay_run_id": replay_run_id, "period": [ds.spec["start"], ds.spec["end"]],
                   "symbols": ds.spec["symbols"], "timeframe": ds.spec["timeframe"],
                   "cost_model": run_cfg.costs.canonical(), "funding_sources": res.funding_sources,
                   "skipped_signals": dict(sorted(res.skipped.items())), "data_quality": ds.quality_summary,
                   "halted_at": res.halted_at.isoformat() if res.halted_at else None,
                   "warnings": warnings, "limitations": LIMITATIONS}
        config = {"pipeline": gate.pipeline_id, "rule_name": gate.pipeline_id,
                  "initial_equity_quote": run_cfg.initial_equity_quote, "run": run_cfg.canonical(),
                  "gate": gate.config}
        trades = [t.to_dict() for t in res.trades]
        decisions = {"pipeline": gate.pipeline_id, "decisions": res.decisions}
        created = self.store.save_run(
            run_id=res.run_id, kind="backtest", parent_id=replay_run_id, dataset_id=ds.dataset_id, config=config,
            versions=dict(ds.versions), summary=summary,
            full_result={"summary": summary, "metrics": metrics, "trades": trades, "splits": split,
                         "decisions": decisions, "equity": [(t.isoformat(), v) for t, v in res.equity]},
            trades=trades, equity=res.equity,
            reports=[("metrics", metrics), ("splits", split), ("decisions", decisions)])
        return res, {"summary": summary, "metrics": metrics, "splits": split, "decisions": decisions}, created
