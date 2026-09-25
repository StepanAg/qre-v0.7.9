"""Phase 8 analytics engine: pure computations over domain objects supplied by an
AnalyticsSource. Read-only, deterministic (no clock, no randomness, no network).
Metric format = app.research.lab.stats ({"value"} | {"status": "not_available", "reason"})."""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from typing import Callable, Sequence

from app.analytics import distributions as dist
from app.analytics import drawdown as dd
from app.analytics.excursions import trade_excursions
from app.domain.market import Timeframe
from app.domain.research_lab import SimulatedTrade
from app.research.lab import stats

BREAKEVEN_EPS = 1e-9            # |net| <= eps -> breakeven: neither a win nor a loss (TZ 5.0)
PNL_TOLERANCE = 1e-6            # relative tolerance of net = gross - fees + funding (TZ 5.3)


# ------------------------------------------------------------------ helpers
def outcome_class(net: float) -> str:
    if net > BREAKEVEN_EPS:
        return "win"
    if net < -BREAKEVEN_EPS:
        return "loss"
    return "breakeven"


def _need(n: int, m: dict) -> dict:
    return m if n else stats.na("insufficient_data")


def pnl_consistent(gross: float, fees: float, funding: float, net: float) -> bool:
    return abs(net - (gross - fees + funding)) <= PNL_TOLERANCE * max(1.0, abs(net))


# -------------------------------------------------------------- 5.1 performance
def performance(trades: Sequence[SimulatedTrade], initial_equity: float | None = None,
                equity: Sequence[tuple] = ()) -> dict:
    n = len(trades)
    nets = [t.net_pnl for t in trades]
    cls = [outcome_class(x) for x in nets]
    wins = [x for x, c in zip(nets, cls) if c == "win"]
    losses = [x for x, c in zip(nets, cls) if c == "loss"]
    rs = [t.r_multiple for t in trades if t.r_multiple is not None]
    gp, gl = sum(wins), sum(losses)
    m = {
        "trades": stats.value(n),
        "wins": stats.value(len(wins)), "losses": stats.value(len(losses)),
        "breakeven": stats.value(cls.count("breakeven")),
        "net_pnl": _need(n, stats.value(sum(nets))),
        "price_gross_pnl": _need(n, stats.value(sum(t.gross_pnl for t in trades))),
        "gross_profit": _need(n, stats.value(gp)), "gross_loss": _need(n, stats.value(gl)),
        "average_trade": stats.mean(nets, "trades"), "median_trade": stats.median(nets, "trades"),
        "best_trade": _need(n, stats.value(max(nets) if nets else 0)),
        "worst_trade": _need(n, stats.value(min(nets) if nets else 0)),
        "win_rate": stats.ratio(len(wins), n, "trades"),
        "breakeven_rate": stats.ratio(cls.count("breakeven"), n, "trades"),
        "expectancy": stats.mean(nets, "trades"),
        "expectancy_r": stats.mean(rs, "r_multiples"),
        "median_r": stats.median(rs, "r_multiples"),
    }
    if not n:
        m["profit_factor"] = m["payoff_ratio"] = stats.na("insufficient_data")
    else:
        m["profit_factor"] = stats.value(gp / abs(gl)) if losses else stats.na("no_losing_trades")
        if not wins:
            m["payoff_ratio"] = stats.na("no_winning_trades")
        elif not losses:
            m["payoff_ratio"] = stats.na("no_losing_trades")
        else:
            m["payoff_ratio"] = stats.value((gp / len(wins)) / abs(gl / len(losses)))
    if initial_equity and n:
        m["return_pct"] = stats.value(sum(nets) / initial_equity)
    else:
        m["return_pct"] = stats.na("insufficient_data" if not n else "no_initial_equity")
    eq = [v for _, v in equity]
    m["cumulative_return"] = stats.value(eq[-1] / eq[0] - 1) if len(eq) >= 2 and eq[0] \
        else stats.na("equity_curve_needs_2_points")
    warnings = []
    w = dist.warning(n)
    if w:
        warnings.append(w)
    if n and sum(nets) > 0 and rs and statistics.median(rs) < 0:
        warnings.append("WARNING: outliers_mask_typical_trade (net > 0 but median R < 0)")
    return {"metrics": m, "warnings": warnings}


# ------------------------------------------------------------------- 5.2 risk
def risk(trades: Sequence[SimulatedTrade], equity: Sequence[tuple] = ()) -> dict:
    rs = [t.r_multiple for t in trades if t.r_multiple is not None]
    m = {"initial_risk": dist.distribution([t.initial_risk for t in trades], "trades"),
         "r_distribution": dist.distribution(rs, "r_multiples"),
         "mean_r": stats.mean(rs, "r_multiples"), "median_r": stats.median(rs, "r_multiples"),
         "stdev_r": stats.value(statistics.stdev(rs)) if len(rs) >= 2 else stats.na("sample_too_small_for:stdev(n<2)"),
         "r_not_available": stats.value(sum(1 for t in trades if t.r_multiple is None))}
    d = dd.analyze(list(equity))
    m["max_drawdown"], m["max_drawdown_pct"] = d["max_drawdown"], d["max_drawdown_pct"]
    ep = d.get("max_drawdown_episode")
    m["max_drawdown_duration_s"] = ep["duration_s"] if ep else stats.na("no_drawdown_episode" if len(equity) >= 2
                                                                         else "equity_curve_needs_2_points")
    m["max_drawdown_recovery_s"] = ep["recovery_duration_s"] if ep else m["max_drawdown_duration_s"]
    return {"metrics": m, "warnings": [w for w in [dist.warning(len(rs))] if w]}


# --------------------------------------------------------------- 5.8 curves
def curves(trades: Sequence[SimulatedTrade], equity: Sequence[tuple]) -> dict:
    """equity (MTM, sim_equity), cumulative R by exit_time, drawdown curve."""
    cum, r_curve = 0.0, []
    for t in sorted(trades, key=lambda x: (x.exit_time, x.trade_no)):
        if t.r_multiple is not None:
            cum += t.r_multiple
            r_curve.append({"t": t.exit_time.isoformat(), "trade_no": t.trade_no, "cumulative_r": cum})
    return {"equity": [{"t": a.isoformat(), "equity": b} for a, b in equity], "cumulative_r": r_curve,
            "drawdown": dd.drawdown_curve(list(equity))}


# ------------------------------------------------------------------ 5.3 costs
def costs(trades: Sequence[SimulatedTrade]) -> dict:
    """Phase 0 accounting model: net = gross - fees + funding; slippage is already inside
    gross (attribution only); theoretical_gross = gross + slippage_cost."""
    n = len(trades)
    gross = sum(t.gross_pnl for t in trades)
    slip = sum(t.slippage_cost for t in trades)
    fees = sum(t.fees for t in trades)
    fund = sum(t.funding for t in trades)
    theo = gross + slip
    bad = [t.trade_no for t in trades if not pnl_consistent(t.gross_pnl, t.fees, t.funding, t.net_pnl)]
    mix: dict[str, int] = {}
    for t in trades:
        mix[t.funding_source] = mix.get(t.funding_source, 0) + 1
    m = {"theoretical_gross": _need(n, stats.value(theo)), "slippage_cost": _need(n, stats.value(slip)),
         "price_gross": _need(n, stats.value(gross)), "fees": _need(n, stats.value(fees)),
         "funding": _need(n, stats.value(fund)), "net": _need(n, stats.value(sum(t.net_pnl for t in trades))),
         "cost_share": stats.ratio(fees + slip - fund, abs(theo), "theoretical_gross") if n
         else stats.na("insufficient_data"),
         "funding_source_mix": stats.value(dict(sorted(mix.items()))),
         "pnl_inconsistent_trades": stats.value(bad)}
    warnings = []
    if mix.get("assumed"):
        warnings.append("FUNDING_ASSUMED: part of the funding is a configured assumption")
    if bad:
        warnings.append(f"PNL_INCONSISTENT: trades {bad}")
    return {"metrics": m, "warnings": warnings}


# ------------------------------------------------------- 5.4 excursions summary
def excursions(trades: Sequence[SimulatedTrade], candles_of, tf: Timeframe) -> dict:
    """candles_of(symbol, start, end) -> closed candles; one call per trade window."""
    rows = [trade_excursions(t, candles_of(t.symbol, t.entry_time, t.exit_time), tf) for t in trades]
    ok = [r for r in rows if r["status"] == "valid"]
    val = lambda k: [r[k]["value"] for r in ok if "value" in r[k]]  # noqa: E731
    summary = {"valid": len(ok), "not_available": len(rows) - len(ok),
               "mfe_pct": dist.distribution([r["mfe_pct"] for r in ok], "trades"),
               "mae_pct": dist.distribution([r["mae_pct"] for r in ok], "trades"),
               "mfe_r": dist.distribution(val("mfe_r"), "trades"), "mae_r": dist.distribution(val("mae_r"), "trades"),
               "capture_ratio": dist.distribution(val("capture_ratio"), "trades"),
               "entry_efficiency": dist.distribution(val("entry_efficiency"), "trades"),
               "exit_efficiency": dist.distribution(val("exit_efficiency"), "trades"),
               "adverse_first_share": stats.ratio(sum(1 for r in ok if r["adverse_first"] is True),
                                                  len(ok), "valid_windows"),
               "late_entry": stats.na("source_not_available"),          # needs an Entry Zone engine
               "side_aware_quotes": stats.na("source_not_available")}   # bid/ask not recorded
    return {"trades": rows, "summary": summary,
            "limitations": ["bar_resolution_upper_bound: exit-bar high/low may include post-exit movement"]}


# ------------------------------------------------------------------ 5.5 exits
EXIT_ORDER = ("stop", "stop_ambiguous", "take_profit", "time", "end_of_data")


def exits(trades: Sequence[SimulatedTrade], exc_rows: Sequence[dict] = ()) -> dict:
    by_no = {r["trade_no"]: r for r in exc_rows if r.get("status") == "valid"}
    groups = {}
    for reason in EXIT_ORDER:
        ts = [t for t in trades if t.exit_reason.value == reason]
        if not ts:
            continue
        rs = [t.r_multiple for t in ts if t.r_multiple is not None]
        ex = [by_no[t.trade_no] for t in ts if t.trade_no in by_no]
        groups[reason] = {
            "count": stats.value(len(ts)), "net_pnl": stats.value(sum(t.net_pnl for t in ts)),
            "mean_r": stats.mean(rs, "r_multiples"), "median_r": stats.median(rs, "r_multiples"),
            "median_mae_pct": stats.median([e["mae_pct"] for e in ex], "excursions"),
            "median_mfe_pct": stats.median([e["mfe_pct"] for e in ex], "excursions"),
            "median_holding_s": stats.median([(t.exit_time - t.entry_time).total_seconds() for t in ts], "trades")}
        if reason == "end_of_data":
            groups[reason]["note"] = "not closed by the rule: marked to the last close of the dataset"
    return {"groups": groups, "warnings": [w for w in [dist.warning(len(trades))] if w]}


# ------------------------------------------------------------ 5.6 groupings
HOLDING_BUCKETS = ((timedelta(minutes=30), "<=30m"), (timedelta(hours=6), "30m-6h"), (timedelta(hours=18), "6h-18h"))


def holding_bucket(t: SimulatedTrade) -> str:
    held = t.exit_time - t.entry_time
    for limit, name in HOLDING_BUCKETS:
        if held <= limit:
            return name
    return ">18h"


def trade_sequence_drawdown(trades: Sequence[SimulatedTrade]) -> dict:
    """Drawdown of cumulative realized net in exit order (group-level, no equity curve needed)."""
    ordered = sorted(trades, key=lambda x: (x.exit_time, x.trade_no))
    pts, cum = [(ordered[0].entry_time, 0.0)] if ordered else [], 0.0
    for t in ordered:
        cum += t.net_pnl
        pts.append((t.exit_time, cum))
    return dd.analyze(pts)["max_drawdown"]


def group_trades(trades: Sequence[SimulatedTrade], key: Callable[[SimulatedTrade], str],
                 exc_rows: Sequence[dict] = ()) -> dict:
    by_no = {r["trade_no"]: r for r in exc_rows if r.get("status") == "valid"}
    buckets: dict[str, list[SimulatedTrade]] = {}
    for t in trades:
        buckets.setdefault(key(t), []).append(t)
    out = {}
    for name in sorted(buckets):
        ts = buckets[name]
        p = performance(ts)["metrics"]
        ex = [by_no[t.trade_no] for t in ts if t.trade_no in by_no]
        g = {"trades": p["trades"], "net_pnl": p["net_pnl"], "expectancy": p["expectancy"], "median_r": p["median_r"],
             "profit_factor": p["profit_factor"], "trade_sequence_drawdown": trade_sequence_drawdown(ts),
             "median_mae_pct": stats.median([e["mae_pct"] for e in ex], "excursions"),
             "median_mfe_pct": stats.median([e["mfe_pct"] for e in ex], "excursions")}
        w = dist.warning(len(ts))
        if w:
            g["warning"] = w
        out[name] = g
    return out


def live_regimes(rows: Sequence[dict]) -> dict:
    """Distribution of live regime snapshots + regime changes between consecutive snapshots."""
    def counts(k):
        c: dict[str, int] = {}
        for r in rows:
            c[str(r[k])] = c.get(str(r[k]), 0) + 1
        return dict(sorted(c.items()))
    changes = 0
    prev: dict[tuple, str] = {}
    for r in sorted(rows, key=lambda x: (x["symbol"], x["timeframe"], x["as_of_ms"])):
        k = (r["symbol"], r["timeframe"])
        if k in prev and prev[k] != r["regime"]:
            changes += 1
        prev[k] = r["regime"]
    unknown = [r for r in rows if r["regime"] == "unknown"]
    return {"snapshots": stats.value(len(rows)), "regime": counts("regime"),
            "volatility_state": counts("volatility_state"), "btc_regime": counts("btc_regime"),
            "regime_changes": stats.value(changes),
            "unknown_share": stats.ratio(len(unknown), len(rows), "snapshots"),
            "unknown_by_data_quality": {q: sum(1 for r in unknown if r["data_quality"] == q)
                                        for q in sorted({r["data_quality"] for r in unknown})}}


def live_setups_by_regime(setups: Sequence[dict], regimes: Sequence[dict]) -> dict:
    """Join on (symbol, timeframe, as_of_ms == setup_time_ms): the monitor writes both on one bar."""
    at = {(r["symbol"], r["timeframe"], r["as_of_ms"]): r["regime"] for r in regimes}
    out: dict[str, int] = {}
    for s in setups:
        reg = at.get((s["symbol"], s["timeframe"], s["setup_time_ms"]), "unknown_no_snapshot")
        out[reg] = out.get(reg, 0) + 1
    return dict(sorted(out.items()))


# ------------------------------------------------------------- 5.10 portfolio
def portfolio(trades: Sequence[SimulatedTrade], equity: Sequence[tuple] = ()) -> dict:
    if not trades:
        na = stats.na("insufficient_data")
        return {"max_concurrent": na, "avg_concurrent": na, "concurrency_share": {}, "peak_initial_risk": na,
                "peak_notional": na, "max_same_direction": na, "realized_net": na,
                "portfolio_drawdown": dd.analyze(list(equity))["max_drawdown"],
                "leverage": stats.na("source_not_available"), "correlation": stats.na("source_not_available")}
    ev = sorted({t.entry_time for t in trades} | {t.exit_time for t in trades})
    span = (ev[-1] - ev[0]).total_seconds()
    share: dict[str, float] = {}
    weighted = 0.0
    peak = peak_risk = peak_notional = peak_dir = 0
    for a, b in zip(ev, ev[1:]):
        open_ = [t for t in trades if t.entry_time <= a < t.exit_time]
        n = len(open_)
        w = (b - a).total_seconds()
        weighted += n * w
        key = str(n) if n < 3 else ">=3"
        share[key] = share.get(key, 0.0) + w
        peak = max(peak, n)
        peak_risk = max(peak_risk, sum(t.initial_risk for t in open_))
        peak_notional = max(peak_notional, sum(t.entry_price * t.qty for t in open_))
        for d in ("bullish", "bearish"):
            peak_dir = max(peak_dir, sum(1 for t in open_ if t.direction == d))
    return {"max_concurrent": stats.value(peak), "avg_concurrent": stats.ratio(weighted, span, "time_span"),
            "concurrency_share": {k: v / span for k, v in sorted(share.items())} if span else {},
            "peak_initial_risk": stats.value(peak_risk), "peak_notional": stats.value(peak_notional),
            "max_same_direction": stats.value(peak_dir), "realized_net": stats.value(sum(t.net_pnl for t in trades)),
            "portfolio_drawdown": dd.analyze(list(equity))["max_drawdown"],
            "leverage": stats.na("source_not_available"), "correlation": stats.na("source_not_available")}


# --------------------------------------------------------------- 5.11 periods
def _period_start(t: datetime, by: str) -> datetime:
    d = t.replace(hour=0, minute=0, second=0, microsecond=0)
    if by == "day":
        return d
    if by == "week":
        return d - timedelta(days=d.weekday())                  # ISO week starts on Monday
    if by == "month":
        return d.replace(day=1)
    raise ValueError("--by must be day, week or month")


def _next(p: datetime, by: str) -> datetime:
    if by == "day":
        return p + timedelta(days=1)
    if by == "week":
        return p + timedelta(days=7)
    return (p.replace(day=28) + timedelta(days=4)).replace(day=1)


def periods(trades: Sequence[SimulatedTrade], equity: Sequence[tuple], by: str,
            initial_equity: float | None) -> list[dict]:
    if not trades:
        return []
    first = _period_start(min(t.exit_time for t in trades), by)
    last = _period_start(max(t.exit_time for t in trades), by)
    out, p = [], first
    while p <= last:
        q = _next(p, by)
        ts = [t for t in trades if p <= t.exit_time < q]
        before = [v for a, v in equity if a <= p]
        base = before[-1] if before else initial_equity
        inside = ([(p, base)] if base is not None else []) + [(a, v) for a, v in equity if p < a < q]
        perf = performance(ts)["metrics"]
        c = costs(ts)["metrics"]
        out.append({"period": p.date().isoformat(), "by": by, "trades": perf["trades"], "net_pnl": perf["net_pnl"],
                    "return": stats.ratio(sum(t.net_pnl for t in ts), base, "period_start_equity")
                    if ts and base else stats.na("insufficient_data" if not ts else "no_period_start_equity"),
                    "median_r": perf["median_r"], "expectancy": perf["expectancy"],
                    "profit_factor": perf["profit_factor"], "max_drawdown": dd.analyze(inside)["max_drawdown"],
                    "fees": c["fees"], "funding": c["funding"], "slippage": c["slippage_cost"]})
        p = q
    return out


# ---------------------------------------------------------------- 5.7 funnels
RULE_FILTER_SKIPS = ("type_not_in_rule", "not_confirmed", "regime_fit_not_allowed")
EXECUTION_SKIPS = ("no_next_bar", "position_already_open", "stop_on_wrong_side", "opened_beyond_stop", "no_atr")


def _steps(pairs: Sequence[tuple[str, int]]) -> list[dict]:
    out, prev = [], None
    for name, n in pairs:
        row = {"step": name, "count": stats.value(n)}
        if prev is not None:
            if n > prev:                                   # impossible for a funnel: inconsistent data
                row["conversion"] = row["rejection"] = stats.na("count_exceeds_previous_step")
            else:
                row["conversion"] = stats.ratio(n, prev, f"previous_step:{name}")
                row["rejection"] = stats.ratio(prev - n, prev, f"previous_step:{name}")
        out.append(row)
        prev = n
    return out


PRE_STRATEGY_SKIPS = ("no_next_bar", "position_already_open")     # Phase 9 loop order


def _decision_funnel(observations: Sequence[dict], skipped: dict, trades: Sequence[SimulatedTrade]) -> dict:
    """Phase 9 Strategy + Risk runs: steps in the order the decision loop applies them."""
    observed = len(observations)
    decidable = observed - skipped.get("not_decidable", 0)
    evaluated = decidable - sum(skipped.get(k, 0) for k in PRE_STRATEGY_SKIPS)
    strat = {k: v for k, v in skipped.items() if k.startswith("strategy:")}
    risk_ = {k: v for k, v in skipped.items() if k.startswith("risk:")}
    accepted = evaluated - sum(strat.values())
    approved = accepted - sum(risk_.values())
    entered_by_funnel = approved - skipped.get("opened_beyond_stop", 0)
    profitable = sum(1 for t in trades if outcome_class(t.net_pnl) == "win")
    known = {"not_decidable", "opened_beyond_stop", *PRE_STRATEGY_SKIPS}
    unknown = sorted(k for k in skipped if k not in known and not k.startswith(("strategy:", "risk:")))
    warnings = []
    if entered_by_funnel != len(trades):
        warnings.append(f"FUNNEL_INCONSISTENT: skips imply {entered_by_funnel} entries, sim_trades holds {len(trades)}")
    if unknown:
        warnings.append(f"UNKNOWN_SKIP_REASONS: {unknown}")
    return {"steps": _steps([("observed_setups", observed), ("decidable", decidable),
                             ("evaluated", evaluated), ("strategy_accepted", accepted),
                             ("risk_approved", approved), ("entered", len(trades)), ("profitable", profitable)]),
            "pre_strategy_skips": {k: skipped.get(k, 0) for k in ("not_decidable", *PRE_STRATEGY_SKIPS)},
            "strategy_rejections": dict(sorted(strat.items())), "risk_rejections": dict(sorted(risk_.items())),
            "execution_skips": {"opened_beyond_stop": skipped.get("opened_beyond_stop", 0)},
            "not_recorded_stages": {"ai_approved": stats.na("source_not_available"),
                                    "executed_real": stats.na("source_not_available")},
            "warnings": warnings}


def research_funnel(observations: Sequence[dict], backtest_summary: dict,
                    trades: Sequence[SimulatedTrade]) -> dict:
    skipped = dict(backtest_summary.get("skipped_signals", {}))
    if backtest_summary.get("pipeline") == "strategy_risk_v1":
        return _decision_funnel(observations, skipped, trades)
    unknown = sorted(set(skipped) - set(RULE_FILTER_SKIPS) - set(EXECUTION_SKIPS))
    observed = len(observations)
    passed = observed - sum(skipped.get(k, 0) for k in RULE_FILTER_SKIPS)
    entered_by_funnel = passed - sum(skipped.get(k, 0) for k in EXECUTION_SKIPS)
    profitable = sum(1 for t in trades if outcome_class(t.net_pnl) == "win")
    warnings = []
    if entered_by_funnel != len(trades):
        warnings.append(f"FUNNEL_INCONSISTENT: skips imply {entered_by_funnel} entries, sim_trades holds {len(trades)}")
    if unknown:
        warnings.append(f"UNKNOWN_SKIP_REASONS: {unknown}")
    return {"steps": _steps([("observed_setups", observed), ("passed_rule_filters", passed),
                             ("entered", len(trades)), ("profitable", profitable)]),
            "rule_filter_skips": {k: skipped.get(k, 0) for k in RULE_FILTER_SKIPS},
            "execution_skips": {k: skipped.get(k, 0) for k in EXECUTION_SKIPS},
            "not_recorded_stages": {"ai_approved": stats.na("source_not_available"),
                                    "risk_approved": stats.na("source_not_available"),
                                    "executed_real": stats.na("source_not_available")},
            "warnings": warnings}


def live_setup_funnel(setups: Sequence[dict], events: Sequence[dict]) -> dict:
    ev: dict[str, dict[str, datetime]] = {}
    for e in events:
        ev.setdefault(e["setup_id"], {})[e["status"]] = e["at"]

    def one(rows):
        n = len(rows)
        st = [ev.get(r["setup_id"], {}) for r in rows]
        confirmed = sum(1 for x in st if "confirmed" in x)
        inval = sum(1 for x in st if "invalidated" in x)
        expired = sum(1 for x in st if "expired" in x)
        waits = [(x["confirmed"] - x["candidate"]).total_seconds() for x in st if "confirmed" in x and "candidate" in x]
        return {"candidates": stats.value(n), "confirmed": stats.value(confirmed),
                "invalidated": stats.value(inval), "expired": stats.value(expired),
                "still_active": stats.value(n - inval - expired),
                "confirmation_rate": stats.ratio(confirmed, n, "candidates"),
                "median_time_to_confirm_s": stats.median(waits, "confirmed_setups")}
    groups: dict[str, list[dict]] = {}
    for r in setups:
        groups.setdefault(f"{r['setup_type']}/{r['direction']}/{r['timeframe']}", []).append(r)
    return {"total": one(list(setups)), "groups": {k: one(v) for k, v in sorted(groups.items())},
            "not_recorded_stages": {"ai_approved": stats.na("source_not_available"),
                                    "risk_approved": stats.na("source_not_available"),
                                    "executed_real": stats.na("source_not_available")}}


# ------------------------------------------------------------- 5.9 untraded
def untraded(outcomes: Sequence, trades: Sequence[SimulatedTrade], tf: Timeframe) -> dict:
    """POST_SETUP_ANALYSIS: what price did after setups the rule did NOT trade.
    Never mixed with trade statistics; hypothetical R needs a stop outcomes do not have."""
    traded = {t.setup_id for t in trades}
    rows = [o for o in outcomes if o.setup_id not in traded]
    by_h: dict[int, list] = {}
    for o in rows:
        by_h.setdefault(o.horizon, []).append(o)
    out = {}
    for h in sorted(by_h):
        os_ = by_h[h]
        valid = [o for o in os_ if o.status.value == "valid"]
        col = lambda k: [o.values[k] for o in valid if o.values.get(k) is not None]  # noqa: E731
        firsts = col("first_1atr")
        out[str(h)] = {"horizon_bars": h, "horizon_minutes": h * tf.delta.total_seconds() / 60,
                       "counts": {st: sum(1 for o in os_ if o.status.value == st)
                                  for st in ("valid", "censored", "incomplete", "no_anchor")},
                       "median_forward_return": stats.median(col("forward_return"), "observations"),
                       "median_mfe": stats.median(col("mfe"), "observations"),
                       "median_mae": stats.median(col("mae"), "observations"),
                       "median_mfe_atr": stats.median(col("mfe_atr"), "observations"),
                       "median_mae_atr": stats.median(col("mae_atr"), "observations"),
                       "first_1atr": {k: firsts.count(k) for k in ("favorable", "adverse", "ambiguous", "none")},
                       "hypothetical_r": stats.na("no_stop_defined")}
    return {"label": "POST_SETUP_ANALYSIS", "untraded_setups": len({o.setup_id for o in rows}),
            "by_horizon": out, "note": "not trade statistics; setups the rule did not trade"}


# ------------------------------------------------- section 3: version segregation
def run_version_status(run: dict, current_versions: dict) -> str:
    return "current" if dict(run.get("versions", {})) == dict(current_versions) else "version_mismatch"


def guard_run(run: dict, current_versions: dict, include_stale: bool) -> tuple[dict | None, list[str]]:
    """(None, warnings) when the run may be used; (not_available, []) when it is excluded."""
    if run_version_status(run, current_versions) == "current":
        return None, []
    if include_stale:
        return None, [f"VERSION_MISMATCH: run {run['run_id']} was produced with component versions "
                      f"{run.get('versions')} (current {current_versions}); included on request"]
    return stats.na("version_mismatch"), []


def guard_mixed(rows: Sequence[dict], keys: Sequence[str], allow: bool) -> tuple[dict | None, list[str]]:
    combos = sorted({tuple(str(r.get(k)) for k in keys) for r in rows})
    if len(combos) <= 1:
        return None, []
    if allow:
        return None, [f"MIXED_CONFIGS: {len(combos)} {'/'.join(keys)} combinations mixed on request: {combos}"]
    return stats.na("mixed_configs"), []


# ------------------------------------------------------ 5.12 compare backtests
def compare_runs(runs: Sequence[dict], trades_by_run: dict[str, Sequence[SimulatedTrade]],
                 equity_by_run: dict[str, Sequence[tuple]], force: bool = False) -> dict:
    if len(runs) < 2:
        raise ValueError("compare needs at least two backtest runs")
    ds = {r["dataset_id"] for r in runs}
    vs = {tuple(sorted(r["versions"].items())) for r in runs}
    reasons = (["different_datasets"] if len(ds) > 1 else []) + (["different_versions"] if len(vs) > 1 else [])
    comparable = not reasons
    out = {"comparable": comparable, "reasons": reasons, "runs": {}}
    if not comparable and not force:
        out["note"] = "metrics withheld: runs are not comparable (use --force-incomparable to show them)"
        return out
    for r in runs:
        rid = r["run_id"]
        ts = trades_by_run.get(rid, [])
        p = performance(ts, r["config"].get("initial_equity_quote"), equity_by_run.get(rid, ()))["metrics"]
        c = costs(ts)["metrics"]
        rk = risk(ts, equity_by_run.get(rid, ()))["metrics"]
        out["runs"][rid] = {"rule": r["config"].get("rule_name"), "config_hash_basis": r["config"],
                            "trades": p["trades"], "net_pnl": p["net_pnl"], "expectancy": p["expectancy"],
                            "median_r": p["median_r"], "profit_factor": p["profit_factor"],
                            "win_rate": p["win_rate"], "fees": c["fees"], "funding": c["funding"],
                            "max_drawdown": rk["max_drawdown"]}
    if not comparable:
        out["warning"] = f"NOT COMPARABLE ({', '.join(reasons)}): shown only because --force-incomparable was given"
    out["note"] = "side-by-side facts only; no run is selected as better"
    return out


# ======================================================= orchestration (CLI facade)
class AnalyticsEngine:
    """Binds the pure functions above to an AnalyticsSource. Read-only; every answer is
    an envelope {source, filters, as_of, versions, warnings, data} (see report.py)."""

    def __init__(self, src, current_versions: dict) -> None:
        self.src = src
        self.current = dict(current_versions)

    # ------------------------------------------------------------ research runs
    def backtest_context(self, run_id: str, flt=None, include_stale: bool = False) -> dict:
        from app.analytics.filters import AnalyticsFilter
        flt = flt or AnalyticsFilter()
        run = self.src.run(run_id)
        if run is None:
            raise LookupError(f"research run {run_id} not found")
        if run["kind"] != "backtest":
            raise ValueError(f"run {run_id} is a {run['kind']} run; analytics of trades needs a backtest run")
        blocked, warns = guard_run(run, self.current, include_stale)
        ds = self.src.dataset(run["dataset_id"]) or {}
        tf = Timeframe(ds.get("spec", {}).get("timeframe", "15m"))
        obs = self.src.observations(run["parent_id"])
        fit = {o["setup"].setup_id: o["first_regime_fit"] for o in obs}
        all_trades = self.src.trades(run_id)
        trades = flt.trades(all_trades, timeframe=tf.value, regime_fit_of=fit)
        filtered = bool(flt.used())
        equity = [] if filtered else self.src.equity(run_id)
        if filtered:
            warns.append("FILTERED: equity-derived metrics are not_available (the equity curve covers the whole run)")
        return {"run": run, "dataset": ds, "tf": tf, "observations": obs, "fit": fit, "trades": trades,
                "all_trades": all_trades, "equity": equity, "filtered": filtered, "blocked": blocked,
                "warnings": warns, "filters": flt.used(), "source": f"research:{run_id}"}

    @staticmethod
    def as_of(ctx: dict) -> str | None:
        ts = [t.exit_time for t in ctx["trades"]] + [a for a, _ in ctx["equity"]]
        return max(ts).isoformat() if ts else None

    def _candles_of(self, tf: Timeframe):
        return lambda sym, a, b: self.src.candles(sym, tf.value, a, b)

    def research(self, command: str, run_id: str, flt=None, include_stale: bool = False, **kw) -> dict:
        ctx = self.backtest_context(run_id, flt, include_stale)
        env = {"source": ctx["source"], "filters": ctx["filters"], "as_of": self.as_of(ctx),
               "versions": ctx["run"]["versions"], "warnings": list(ctx["warnings"])}
        if ctx["blocked"]:
            return {**env, "data": ctx["blocked"]}
        ts, eq, tf = ctx["trades"], ctx["equity"], ctx["tf"]
        init = ctx["run"]["config"].get("initial_equity_quote")
        na_eq = stats.na("equity_not_filterable")
        exc = None
        if command in ("trades", "exits", "setups", "report"):
            exc = excursions(ts, self._candles_of(tf), tf)
        if command == "performance":
            p = performance(ts, init, eq)
            if ctx["filtered"]:
                p["metrics"]["cumulative_return"] = na_eq
            data, env["warnings"] = p["metrics"], env["warnings"] + p["warnings"]
        elif command == "risk":
            r = risk(ts, eq)
            if ctx["filtered"]:
                for k in ("max_drawdown", "max_drawdown_pct", "max_drawdown_duration_s", "max_drawdown_recovery_s"):
                    r["metrics"][k] = na_eq
            data, env["warnings"] = r["metrics"], env["warnings"] + r["warnings"]
        elif command == "costs":
            c = costs(ts)
            data, env["warnings"] = c["metrics"], env["warnings"] + c["warnings"]
        elif command == "trades":
            by_no = {r["trade_no"]: r for r in exc["trades"]}
            data = {"trades": [{**t.to_dict(), "excursions": by_no.get(t.trade_no)} for t in ts],
                    "excursion_summary": exc["summary"], "limitations": exc["limitations"]}
        elif command == "exits":
            e = exits(ts, exc["trades"])
            data, env["warnings"] = e["groups"], env["warnings"] + e["warnings"]
        elif command == "drawdown":
            if ctx["filtered"]:
                data = na_eq
            else:
                data = {"analysis": dd.analyze(eq)}
                if kw.get("curve"):
                    key = {"equity": "equity", "r": "cumulative_r", "drawdown": "drawdown"}[kw["curve"]]
                    data["curve"] = {kw["curve"]: curves(ts, eq)[key]}
        elif command == "setups":
            data = {"by_setup_type": group_trades(ts, lambda t: t.setup_type, exc["trades"]),
                    "by_direction": group_trades(ts, lambda t: t.direction, exc["trades"]),
                    "by_symbol": group_trades(ts, lambda t: t.symbol, exc["trades"]),
                    "timeframe": tf.value}
        elif command == "regimes":
            data = {"by_regime_fit": group_trades(ts, lambda t: ctx["fit"].get(t.setup_id, "unknown")),
                    "regime_label": stats.na("source_not_available"),
                    "btc_context": stats.na("source_not_available"),
                    "note": "research trades carry regime_fit only (decision D1); regime labels exist for live data"}
        elif command == "horizons":
            data = {"by_holding_bucket": group_trades(ts, holding_bucket),
                    "holding_time_s": dist.distribution([(t.exit_time - t.entry_time).total_seconds() for t in ts],
                                                        "trades"),
                    "planned_horizon": stats.na("source_not_available"),
                    "horizon_violation": stats.na("source_not_available")}
        elif command == "funnel":
            f = research_funnel(ctx["observations"], ctx["run"]["summary"], ctx["all_trades"])
            data, env["warnings"] = f, env["warnings"] + f.pop("warnings")
            if ctx["filtered"]:
                env["warnings"].append("FILTERS_IGNORED: the funnel always covers the whole run")
        elif command == "untraded":
            outs, orun = [], None
            for r in self.src.list_runs("outcomes"):
                if r["parent_id"] == ctx["run"]["parent_id"]:
                    outs, orun = self.src.outcomes(r["run_id"]), r["run_id"]
                    break
            data = {**untraded(outs, ctx["all_trades"], tf), "outcomes_run": orun} if outs \
                else stats.na("no_outcomes_run_for_this_replay")
        elif command == "portfolio":
            data = portfolio(ts, eq)
            if ctx["filtered"]:
                data["portfolio_drawdown"] = na_eq
        elif command == "periods":
            data = periods(ts, eq, kw.get("by", "month"), init)
        else:
            raise ValueError(f"unknown research command {command}")
        return {**env, "data": data}

    def report(self, run_id: str, include_stale: bool = False) -> dict:
        ctx = self.backtest_context(run_id, None, include_stale)
        if ctx["blocked"]:
            return {"source": ctx["source"], "filters": {}, "as_of": None, "versions": ctx["run"]["versions"],
                    "warnings": ctx["warnings"], "data": ctx["blocked"]}
        parts = {}
        warnings = list(ctx["warnings"])
        for cmd in ("performance", "risk", "costs", "exits", "setups", "regimes", "horizons", "funnel", "untraded",
                    "portfolio"):
            e = self.research(cmd, run_id, include_stale=include_stale)
            parts[cmd] = e["data"]
            warnings += [w for w in e["warnings"] if w not in warnings]
        parts["periods_monthly"] = self.research("periods", run_id, include_stale=include_stale, by="month")["data"]
        parts["drawdown"] = self.research("drawdown", run_id, include_stale=include_stale)["data"]
        dq = self.data_quality()["data"]
        run, ds = ctx["run"], ctx["dataset"]
        return {"source": ctx["source"], "filters": {}, "as_of": self.as_of(ctx), "versions": run["versions"],
                "warnings": warnings,
                "data": {"dataset": {"dataset_id": run["dataset_id"], "spec": ds.get("spec"),
                                     "quality_summary": ds.get("quality_summary")},
                         "period": [ds.get("spec", {}).get("start"), ds.get("spec", {}).get("end")],
                         "rule": run["config"].get("rule_name"), "config": run["config"],
                         "cost_model": run["config"].get("costs"), **parts,
                         "ai": stats.na("source_not_available"),
                         "data_quality": {"counts": dq["counts"], "summary": dq["summary"]},
                         "stored_phase7_reports": "legacy_metric_semantics: breakeven_counted_as_loss "
                                                  "(Phase 8 recomputes every metric from sim_trades)",
                         "limitations": REPORT_LIMITATIONS}}

    # --------------------------------------------------------------------- live
    def live(self, command: str, flt=None, allow_mixed: bool = False) -> dict:
        from app.analytics.filters import AnalyticsFilter
        flt = flt or AnalyticsFilter()
        env = {"source": "live", "filters": flt.used(), "versions": self.current, "warnings": []}
        if command in ("setups", "funnel"):
            rows = flt.live_rows(self.src.live_setups(), "setup_time")
            blocked, w = guard_mixed(rows, ("engine_version", "config_hash"), allow_mixed)
            env["warnings"] += w
            env["as_of"] = max((r["setup_time"] for r in rows), default=None)
            if blocked:
                return {**env, "as_of": env["as_of"] and env["as_of"].isoformat(), "data": blocked}
            ids = {r["setup_id"] for r in rows}
            f = live_setup_funnel(rows, [e for e in self.src.setup_events() if e["setup_id"] in ids])
            data = f if command == "funnel" else {"groups": f["groups"], "total": f["total"]}
        elif command == "regimes":
            regs = flt.live_rows(self.src.regime_snapshots(), "as_of")
            blocked, w = guard_mixed(regs, ("regime_version", "config_hash"), allow_mixed)
            env["warnings"] += w
            env["as_of"] = max((r["as_of"] for r in regs), default=None)
            if blocked:
                return {**env, "as_of": env["as_of"] and env["as_of"].isoformat(), "data": blocked}
            setups = [s for s in self.src.live_setups() if not flt.symbol or s["symbol"] == flt.symbol]
            data = {"distribution": live_regimes(regs), "live_setups_by_regime": live_setups_by_regime(setups, regs)}
        else:
            raise ValueError(f"--live is not supported by {command}")
        env["as_of"] = env["as_of"].isoformat() if env["as_of"] else None
        return {**env, "data": data}

    # -------------------------------------------------------------------- misc
    def compare(self, run_ids: Sequence[str], force: bool = False, include_stale: bool = False) -> dict:
        runs, warns = [], []
        for rid in run_ids:
            r = self.src.run(rid)
            if r is None or r["kind"] != "backtest":
                raise LookupError(f"backtest run {rid} not found")
            blocked, w = guard_run(r, self.current, include_stale)
            if blocked:
                raise ValueError(f"run {rid}: version_mismatch (use --include-stale-versions)")
            warns += w
            runs.append(r)
        tr = {r["run_id"]: self.src.trades(r["run_id"]) for r in runs}
        eq = {r["run_id"]: self.src.equity(r["run_id"]) for r in runs}
        ts = [t.exit_time for v in tr.values() for t in v]
        return {"source": "research:" + ",".join(run_ids), "filters": {}, "as_of": max(ts).isoformat() if ts else None,
                "versions": self.current, "warnings": warns, "data": compare_runs(runs, tr, eq, force)}

    def summary(self) -> dict:
        from app.analytics import sources
        names = self.src.table_names()
        runs = self.src.list_runs()
        counts = {t: self.src.table_count(t) for t in sorted(names) if t != "sqlite_sequence"}
        return {"source": "all", "filters": {}, "as_of": None, "versions": self.current, "warnings": [],
                "data": {"sources": sources.availability(names), "migrations": self.src.migrations(),
                         "research_runs": [{"run_id": r["run_id"], "kind": r["kind"], "dataset_id": r["dataset_id"],
                                            "version_status": run_version_status(r, self.current)} for r in runs],
                         "table_counts": counts,
                         "real_trades_rows": counts.get("trades", 0),
                         "note": "real trades: schema exists, nothing in QRE writes it (source not available)"}}

    def data_quality(self) -> dict:
        from app.analytics import quality
        return {"source": "all", "filters": {}, "as_of": None, "versions": self.current, "warnings": [],
                "data": quality.data_quality(self.src, self.current)}

    def monitor(self) -> dict:
        from app.analytics import quality
        return {"source": "live", "filters": {}, "as_of": None, "versions": self.current, "warnings": [],
                "data": quality.monitor_summary(self.src)}

    def not_available(self, command: str) -> dict:
        missing = {"ai": ["ai.decisions (no AI/LLM layer)", "model/provider/prompt versions"],
                   "rejected-real": ["risk.decisions (no Risk Engine)", "exec.post_exit (no post-exit tracking)",
                                     "real.trades (schema only, no writer)"]}[command]
        return {"source": "none", "filters": {}, "as_of": None, "versions": self.current, "warnings": [],
                "data": {"status": "not_available", "reason": "source_not_available", "missing_components": missing}}


REPORT_LIMITATIONS = [
    "research trades are hypothetical test-rule simulations (Phase 7), not real trades",
    "MAE/MFE at OHLC bar resolution: exit-bar extremes are an upper bound; no bid/ask",
    "research trades carry regime_fit only; regime labels are source_not_available (decision D1)",
    "no margin/leverage model, no Horizon or Entry Zone engine, no AI, no real trades",
    "statistics with n < 30 carry a sample-size warning; nothing here proves profitability",
]
