"""Backtest metrics. Any metric whose denominator is zero or whose data is missing is
reported as not_available with a reason - never an invented number."""
from __future__ import annotations

import statistics
from typing import Sequence

from app.domain.research_lab import SimulatedTrade
from app.research.lab import stats
from app.research.lab.backtest import BacktestResult


def max_drawdown(equity: Sequence[float]) -> tuple[float, float]:
    peak, dd, dd_pct = None, 0.0, 0.0
    for v in equity:
        peak = v if peak is None or v > peak else peak
        dd = max(dd, peak - v)
        if peak > 0:
            dd_pct = max(dd_pct, (peak - v) / peak)
    return dd, dd_pct


def streaks(nets: Sequence[float]) -> tuple[int, int]:
    best_w = best_l = w = l = 0
    for x in nets:
        if x > 0:
            w, l = w + 1, 0
        else:
            w, l = 0, l + 1
        best_w, best_l = max(best_w, w), max(best_l, l)
    return best_w, best_l


def trade_metrics(trades: Sequence[SimulatedTrade], equity: Sequence[float] | None = None,
                  bars: int = 0, position_bars: int = 0) -> dict:
    nets = [t.net_pnl for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    rs = [t.r_multiple for t in trades if t.r_multiple is not None]
    m = {"trades": stats.value(len(trades)),
         "gross_pnl": stats.value(sum(t.gross_pnl for t in trades)),
         "fees": stats.value(sum(t.fees for t in trades)),
         "funding": stats.value(sum(t.funding for t in trades)),
         "slippage_cost_in_gross": stats.value(sum(t.slippage_cost for t in trades)),
         "net_pnl": stats.value(sum(nets)),
         "win_rate": stats.ratio(len(wins), len(nets), "trades"),
         "profit_factor": stats.ratio(sum(wins), abs(sum(losses)), "gross_losses") if losses
         else stats.na("no_losing_trades" if nets else "no_trades"),
         "expectancy_per_trade": stats.mean(nets, "trades"),
         "average_r": stats.mean(rs, "r_multiples"),
         "median_r": stats.median(rs, "r_multiples"),
         "ambiguous_exits": stats.value(sum(1 for t in trades if t.ambiguous)),
         "end_of_data_exits": stats.value(sum(1 for t in trades if t.exit_reason.value == "end_of_data"))}
    if nets:
        w, l = streaks(nets)
        m["max_consecutive_wins"], m["max_consecutive_losses"] = stats.value(w), stats.value(l)
    else:
        m["max_consecutive_wins"] = m["max_consecutive_losses"] = stats.na("no_trades")
    if equity:
        dd, pct = max_drawdown(equity)
        m["max_drawdown"], m["max_drawdown_pct"] = stats.value(dd), stats.value(pct)
    else:
        m["max_drawdown"] = m["max_drawdown_pct"] = stats.na("no_equity_curve")
    m["exposure"] = stats.ratio(position_bars, bars, "bars")
    if len(rs) >= 2:
        m["r_stdev"] = stats.value(statistics.stdev(rs))
    else:
        m["r_stdev"] = stats.na("fewer_than_2_trades")
    return m


def report(res: BacktestResult) -> dict:
    return trade_metrics(res.trades, [e for _, e in res.equity], res.bars, res.position_bars)
