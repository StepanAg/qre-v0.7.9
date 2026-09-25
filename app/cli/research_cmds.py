"""`python -m app research ...` - offline research & backtest on stored data.
Never started by the monitor. Simulated results only: no orders can be placed."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from app.config.settings import load_settings
from app.domain.market import Timeframe
from app.research.lab.config import BacktestConfig, ResearchConfig
from app.research.lab.pipeline import LIMITATIONS, ResearchPipeline
from app.research.setup.config import SetupConfig
from app.research.setup.service import SetupService
from app.research.structure.config import StructureConfig
from app.research.structure.service import StructureService
from app.storage.database import bootstrap
from app.storage.market_data import SQLiteMarketDataStore
from app.storage.research import SQLiteResearchStore
from app.storage.writer import SerializedWriter

BANNER = "RESEARCH / SIMULATION - hypothetical results, not trading performance, not a recommendation."


def build(settings, *, with_regime: bool = False):
    w = SerializedWriter(bootstrap(settings.db_path, shared=True))
    market = SQLiteMarketDataStore(w)
    structure = StructureService(market, StructureConfig.from_file(settings.structure_config_file))
    setups = SetupService(market, structure, SetupConfig.from_file(settings.setup_config_file))
    regime = None
    if with_regime:
        from app.research.regime.config import RegimeConfig
        from app.research.regime.service import RegimeService
        from app.research.service import FeatureService
        regime = RegimeService(FeatureService(market), RegimeConfig.from_file(settings.regime_config_file))
    store = SQLiteResearchStore(w)
    return ResearchPipeline(market, setups, ResearchConfig.from_file(settings.research_config_file), store, regime), \
        store


def _ts(s: str) -> datetime:
    t = datetime.fromisoformat(s)
    if t.tzinfo is None:
        raise ValueError(f"timestamp {s!r} must include a timezone, e.g. 2026-03-01T00:00+00:00")
    return t.astimezone(timezone.utc)


def _metric(m: dict) -> str:
    if "value" in m:
        v = m["value"]
        return f"{v:.6g}" if isinstance(v, float) else str(v)
    return f"n/a ({m['reason']})"


def _print_metrics(metrics: dict) -> None:
    for k, m in metrics.items():
        print(f"  {k:26} {_metric(m)}")


def cmd(a: argparse.Namespace) -> int:
    s = load_settings()
    ctx = bool(getattr(a, "with_context", False) or getattr(a, "with_regime", False))
    pipe, store = build(s, with_regime=ctx)
    c = a.rs_cmd
    if c == "status":
        print(json.dumps({"counts": store.counts(), "runs": store.runs()[:20],
                          "datasets": [d["dataset_id"] for d in store.datasets()][:20]}, indent=2))
        return 0
    if c == "dataset":
        if a.ds_cmd == "build":
            ds, created = pipe.build_dataset(a.symbols, Timeframe.parse(a.tf), _ts(a.start), _ts(a.end),
                                             with_context=a.with_context)
            q = ds.quality_summary
            print(f"dataset {ds.dataset_id} ({'new' if created else 'already stored - identical'})")
            print(f"  {ds.spec['symbols']} {ds.spec['timeframe']} [{ds.spec['start']} .. {ds.spec['end']})")
            print(f"  candles={q['primary_candles']} missing_bars={q['missing_bars']} "
                  f"usable_segments={q['usable_segments']} excluded_segments={len(q['excluded_segments'])}")
            for e in q["excluded_segments"]:
                print(f"    excluded {e['symbol']} {e['start']} .. {e['end']}: {e['reason']}")
            print(f"  versions={ds.versions}")
            return 0
        ds = pipe.dataset(a.id, verify=not a.no_verify)
        print(json.dumps(ds.to_dict(), indent=2))
        return 0
    if c == "replay":
        res, created = pipe.replay(a.dataset, with_regime=a.with_regime)
        print(f"replay {res.run_id} ({'new' if created else 'identical rerun - stored result kept'})")
        print(json.dumps(res.summary(), indent=2))
        return 0
    if c == "outcomes":
        run_id, obs, summary, created = pipe.outcomes(a.replay)
        print(f"outcomes {run_id} ({'new' if created else 'identical rerun'}): {len(obs)} observations "
              "(setups are not trades)")
        for h, d in summary["horizons"].items():
            print(f"  horizon {h:>3}: {d['counts']}  fwd_return median {_metric(d['forward_return']['median'])} "
                  f"MFE median {_metric(d['mfe']['median'])} MAE median {_metric(d['mae']['median'])}")
        return 0
    if c == "decide":
        return _decide(s, pipe, a)
    if c == "decisions":
        r = store.run(a.id)
        if r is None or "decisions" not in r["reports"]:
            print(f"no Strategy + Risk decisions for run {a.id}")
            return 1
        print(BANNER)
        for d in r["reports"]["decisions"]["decisions"]:
            risk = d.get("risk", {})
            print(f"{d.get('decision_time', '-'):26} {d['symbol']:9} {d['setup_id'][:14]:14} {d['outcome']:34} "
                  f"strategy={d.get('strategy', {}).get('reason_code', '-')} risk={risk.get('status', '-')}"
                  + (f" qty={risk['qty']}" if risk.get("qty") not in (None, "0") else ""))
        return 0
    if c == "backtest":
        cfg = BacktestConfig.from_file(a.config or s.backtest_config_file)
        res, rep, created = pipe.backtest(a.replay, cfg)
        print(BANNER)
        print(f"backtest {res.run_id} ({'new' if created else 'identical rerun'}), rule={cfg.rule_name}, "
              f"trades={len(res.trades)}")
        for w in rep["summary"]["warnings"]:
            print(f"  WARNING {w}")
        _print_metrics(rep["metrics"])
        return 0
    if c == "run":
        r = store.run(a.id)
        if r is None:
            print(f"run {a.id} not found")
            return 1
        print(json.dumps({k: r[k] for k in ("run_id", "kind", "parent_id", "dataset_id", "created_at",
                                            "result_hash", "config", "versions", "summary")}, indent=2))
        return 0
    if c == "metrics":
        r = store.run(a.id)
        if r is None or not r["reports"]:
            print(f"no metric reports for {a.id}")
            return 1
        if r["kind"] == "backtest":
            print(BANNER)
            _print_metrics(r["reports"]["metrics"])
            for n in ("train", "validation", "test"):
                sp = r["reports"]["splits"].get(n, {})
                print(f"  [{n}] trades={_metric(sp.get('trades', {'value': 0}))} net={_metric(sp.get('net_pnl', {'value': 0}))}")
        else:
            print(json.dumps(r["reports"], indent=2))
        return 0
    if c == "export":
        r = store.run(a.id)
        if r is None:
            print(f"run {a.id} not found")
            return 1
        doc = {"banner": BANNER, "limitations": LIMITATIONS, "run": r,
               "trades": store.trades(a.id) if r["kind"] == "backtest" else None}
        Path(a.out).write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
        print(f"exported {a.id} -> {a.out}")
        return 0
    return 2


def _decide(s, pipe, a) -> int:
    """Phase 9: Strategy -> Risk Engine over a replay (offline). Instrument rules come from the
    stored `instruments` table and are frozen into the run config; nothing is executed."""
    from decimal import Decimal
    from app.domain.decision import InstrumentRules
    from app.domain.market import Symbol
    from app.research.lab.config import DecisionRunConfig
    from app.risk.config import RiskConfig
    from app.risk.engine import RiskEngineV1
    from app.strategy.config import StrategyConfig
    from app.strategy.engine import StrategyV1
    from app.strategy.pipeline import StrategyRiskGate
    if a.risk_mode == "fixed_quote" and a.fixed_risk_quote is None:
        raise ValueError("--risk-mode fixed_quote needs --fixed-risk-quote (Phase 7 compatibility mode)")
    if a.risk_mode == "equity_pct" and a.fixed_risk_quote is not None:
        raise ValueError("--fixed-risk-quote only applies to --risk-mode fixed_quote")
    run = pipe.store.run(a.replay)
    if run is None or run["kind"] != "replay":
        raise LookupError(f"replay run {a.replay} not found")
    ds = pipe.dataset(run["dataset_id"])
    instruments = {}
    for sym in ds.spec["symbols"]:
        info = pipe.market.instrument(Symbol(sym))
        if info is not None:
            instruments[sym] = InstrumentRules.from_instrument(info)
    risk_cfg = RiskConfig.from_settings(s.risk, a.risk_mode,
                                        None if a.fixed_risk_quote is None else Decimal(str(a.fixed_risk_quote)))
    gate = StrategyRiskGate(StrategyV1(StrategyConfig.from_file(a.strategy_config or s.strategy_config_file)),
                            RiskEngineV1(risk_cfg), instruments)
    res, rep, created = pipe.decide(a.replay, gate, DecisionRunConfig.from_file(a.run_config or
                                                                                  s.decision_run_config_file))
    print(BANNER)
    print(f"strategy+risk {res.run_id} ({'new' if created else 'identical rerun'}), mode={a.risk_mode}, "
          f"trades={len(res.trades)}, decisions={len(res.decisions)}")
    for w in rep["summary"]["warnings"]:
        print(f"  WARNING {w}")
    print("  skipped:", ", ".join(f"{k}={v}" for k, v in rep["summary"]["skipped_signals"].items()) or "-")
    _print_metrics(rep["metrics"])
    return 0


def register(sub) -> None:
    p = sub.add_parser("research", help="offline research & backtest (simulation only)")
    ps = p.add_subparsers(dest="rs_cmd", required=True)
    d = ps.add_parser("dataset", help="build / show a point-in-time research dataset")
    ds = d.add_subparsers(dest="ds_cmd", required=True)
    b = ds.add_parser("build")
    b.add_argument("--symbols", nargs="+", required=True)
    b.add_argument("--tf", required=True)
    b.add_argument("--start", required=True)
    b.add_argument("--end", required=True)
    b.add_argument("--with-context", action="store_true", help="also pin regime timeframes + BTC (for --with-regime)")
    sh = ds.add_parser("show")
    sh.add_argument("--id", required=True)
    sh.add_argument("--no-verify", action="store_true")
    r = ps.add_parser("replay", help="point-in-time replay of the analytics over a dataset")
    r.add_argument("--dataset", required=True)
    r.add_argument("--with-regime", action="store_true")
    o = ps.add_parser("outcomes", help="setup outcome analysis (MAE/MFE, forward returns) of a replay")
    o.add_argument("--replay", required=True)
    bt = ps.add_parser("backtest", help="simulate a hypothetical test rule over a replay")
    bt.add_argument("--replay", required=True)
    bt.add_argument("--config", help="backtest config JSON (default: BACKTEST_CONFIG_FILE)")
    dc = ps.add_parser("decide", help="Phase 9: Strategy -> Risk Engine backtest over a replay (offline)")
    dc.add_argument("--replay", required=True)
    dc.add_argument("--risk-mode", choices=["equity_pct", "fixed_quote"], default="equity_pct")
    dc.add_argument("--fixed-risk-quote", type=float, help="fixed_quote only (Phase 7 compatibility)")
    dc.add_argument("--strategy-config", help="default: STRATEGY_CONFIG_FILE")
    dc.add_argument("--run-config", help="default: DECISION_RUN_CONFIG_FILE")
    ds_ = ps.add_parser("decisions", help="per-setup Strategy and Risk decisions of a run")
    ds_.add_argument("--id", required=True)
    for name, hlp in (("run", "show a run"), ("metrics", "metrics of a run")):
        q = ps.add_parser(name, help=hlp)
        q.add_argument("--id", required=True)
    e = ps.add_parser("export", help="export a run report to JSON")
    e.add_argument("--id", required=True)
    e.add_argument("--out", required=True)
    ps.add_parser("status", help="research subsystem status")
    p.set_defaults(fn=cmd)
