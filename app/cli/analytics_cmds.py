"""`python -m app analytics ...` - read-only analytics over what QRE records.
Opens the database read-only (mode=ro); never writes, never trades."""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from app.analytics import report
from app.analytics.engine import AnalyticsEngine
from app.analytics.filters import AnalyticsFilter
from app.config.settings import load_settings
from app.research.lab.versions import component_versions

RESEARCH_CMDS = ("trades", "performance", "risk", "costs", "exits", "drawdown", "portfolio", "periods", "untraded")
DUAL_CMDS = ("setups", "regimes", "funnel")               # --run <bt> or --live
BANNER = "RESEARCH/LIVE ANALYTICS - facts about recorded data, not trading advice; nothing is written."


def _ts(s: str | None) -> datetime | None:
    if s is None:
        return None
    t = datetime.fromisoformat(s)
    if t.tzinfo is None:
        raise ValueError(f"timestamp {s!r} must include a timezone, e.g. 2026-09-01T00:00+00:00")
    return t


def _filter(a: argparse.Namespace) -> AnalyticsFilter:
    return AnalyticsFilter(start=_ts(getattr(a, "from_", None)), end=_ts(getattr(a, "to", None)),
                           symbol=getattr(a, "symbol", None), direction=getattr(a, "direction", None),
                           setup=getattr(a, "setup", None), regime_fit=getattr(a, "regime_fit", None),
                           regime=getattr(a, "regime", None), timeframe=getattr(a, "timeframe", None),
                           exit_reason=getattr(a, "exit_reason", None))


def cmd(a: argparse.Namespace) -> int:
    s = load_settings()
    if not Path(s.db_path).exists():
        print(f"database {s.db_path} not found - nothing to analyse")
        return 1
    from app.storage.analytics_read import SQLiteAnalyticsReader
    src = SQLiteAnalyticsReader(s.db_path)
    try:
        eng = AnalyticsEngine(src, component_versions())
        c = a.an_cmd
        stale = getattr(a, "include_stale_versions", False)
        if c in ("ai", "rejected-real"):
            env = eng.not_available(c)
        elif c == "summary":
            env = eng.summary()
        elif c == "data-quality":
            env = eng.data_quality()
        elif c == "monitor":
            env = eng.monitor()
        elif c == "compare":
            env = eng.compare(a.runs, force=a.force_incomparable, include_stale=stale)
        elif c == "report":
            env = eng.report(a.run, include_stale=stale)
        elif c in DUAL_CMDS and a.live:
            if a.run:
                raise ValueError("use either --run or --live")
            env = eng.live(c, _filter(a), allow_mixed=a.allow_mixed_configs)
        else:
            if not getattr(a, "run", None):
                raise ValueError(f"analytics {c} needs --run <backtest run id>" +
                                 (" or --live" if c in DUAL_CMDS else ""))
            env = eng.research(c, a.run, _filter(a), include_stale=stale, by=getattr(a, "by", "month"),
                               curve=getattr(a, "curve", None))
        fmt = getattr(a, "format", "text")
        out = report.to_json(env) if fmt == "json" else report.to_csv(c, env) if fmt == "csv" \
            else BANNER + "\n" + report.to_text(c, env)
        if getattr(a, "out", None):
            Path(a.out).write_text(out + "\n", encoding="utf-8")
            print(f"written {a.out}")
        else:
            print(out)
        return 0
    finally:
        src.close()


def _filters(p) -> None:
    p.add_argument("--from", dest="from_", help="inclusive, ISO with timezone")
    p.add_argument("--to", help="exclusive, ISO with timezone")
    p.add_argument("--symbol")
    p.add_argument("--direction", choices=["bullish", "bearish"])
    p.add_argument("--setup", help="setup type, e.g. breakout")
    p.add_argument("--regime-fit", dest="regime_fit", choices=["allowed", "not_allowed", "unknown"])
    p.add_argument("--regime", help="regime label (live data only)")
    p.add_argument("--timeframe")
    p.add_argument("--exit-reason", dest="exit_reason",
                   choices=["stop", "stop_ambiguous", "take_profit", "time", "end_of_data"])


def _common(p, *, run=False, live=False, filters=False) -> None:
    if run:
        p.add_argument("--run", help="backtest run id (research)")
    if live:
        p.add_argument("--live", action="store_true", help="use live data (monitor / setup --save)")
        p.add_argument("--allow-mixed-configs", action="store_true")
    if filters:
        _filters(p)
    p.add_argument("--include-stale-versions", action="store_true")
    p.add_argument("--format", choices=["text", "json", "csv"], default="text")
    p.add_argument("--out", help="write the output to a file")


def register(sub) -> None:
    p = sub.add_parser("analytics", help="read-only analytics (research runs and live data)")
    ps = p.add_subparsers(dest="an_cmd", required=True)
    _common(ps.add_parser("summary", help="sources, runs, counts"))
    for name in RESEARCH_CMDS:
        q = ps.add_parser(name, help=f"{name} of a backtest run")
        _common(q, run=True, filters=True)
        if name == "periods":
            q.add_argument("--by", choices=["day", "week", "month"], default="month")
        if name == "drawdown":
            q.add_argument("--curve", choices=["equity", "r", "drawdown"])
    for name in DUAL_CMDS:
        _common(ps.add_parser(name, help=f"{name}: --run <bt> or --live"), run=True, live=True, filters=True)
    _common(ps.add_parser("horizons", help="holding-time buckets of a backtest run"), run=True, filters=True)
    q = ps.add_parser("compare", help="side-by-side backtest runs (no selection)")
    q.add_argument("--runs", nargs="+", required=True)
    q.add_argument("--force-incomparable", action="store_true")
    _common(q)
    _common(ps.add_parser("monitor", help="monitor runs and live structure events"))
    _common(ps.add_parser("data-quality", help="diagnostic report (nothing is corrected)"))
    q = ps.add_parser("report", help="full research report of a backtest run")
    q.add_argument("--run", required=True)
    _common(q)
    _common(ps.add_parser("ai", help="not available: QRE has no AI layer"))
    _common(ps.add_parser("rejected-real", help="not available: no real trades / risk decisions recorded"))
    p.set_defaults(fn=cmd)
