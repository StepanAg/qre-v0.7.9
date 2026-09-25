"""Isolated, deterministic, event-driven OHLCV backtest of a HYPOTHETICAL test rule.

Not a strategy and not an execution engine: it cannot reach any order path.
Execution model (explicit, conservative):
  * decision at the close of the bar where the setup fact became known;
    entry = market at the OPEN of the NEXT bar (never at the decision bar's own close),
    adverse slippage, taker fee
  * stop = invalidation reference -/+ stop_buffer_atr * ATR(at setup); target = take_profit_r * R
    from the actual fill; both evaluated from the entry bar on
  * bar opens beyond the stop -> filled at that open (gap), adverse slippage, taker fee
  * bar opens beyond the target -> filled at the target (a limit never fills better), maker fee
  * stop AND target inside one bar, order unknown -> the STOP is assumed (flagged ambiguous)
  * time exit after max_hold_bars at the bar close; open at the last bar -> marked to its close (END_OF_DATA)
  * funding: dataset funding rates if available, else the configured assumption (flagged); never both
PnL, costs and R come from the Phase 0 accounting model (Trade/Fill/FundingEvent) with
simulator fills, so fees/funding/slippage are counted exactly once.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Sequence

from app.domain.accounting import FundingEvent, Trade
from app.domain.decision import RiskPlan
from app.domain.enums import (EventSource, FillRole, PositionSide, RegimeFit, Side, SimExitReason, StructureDirection,
                              SetupType)
from app.domain.execution import Fill
from app.domain.market import Candle, FundingRate, Symbol, Timeframe
from app.domain.research_lab import SimulatedTrade, canonical
from app.research.lab.config import BacktestConfig
from app.research.lab.replay import ReplayResult

_BPS = Decimal(10_000)
_FUNDING_HOURS = (0, 8, 16)            # Bybit linear perpetual funding times (UTC) for the ASSUMED model


def _d(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


@dataclass
class _Signal:
    obs_setup: object
    decision_time: datetime
    symbol: str


@dataclass
class _Open:
    sig: _Signal
    trade: Trade
    side: PositionSide
    qty: Decimal
    entry: Decimal
    stop: Decimal
    target: Decimal
    entry_time: datetime
    entry_idx: int
    bars: int = 0
    ambiguous: bool = False


@dataclass
class BacktestResult:
    run_id: str
    replay_run_id: str
    dataset_id: str
    config_hash: str
    trades: list[SimulatedTrade]
    equity: list[tuple[datetime, float]]
    skipped: dict[str, int] = field(default_factory=dict)
    bars: int = 0
    position_bars: int = 0
    funding_sources: dict[str, int] = field(default_factory=dict)


def backtest_run_id(replay_run_id: str, cfg: BacktestConfig) -> str:
    return "bt_" + hashlib.sha256(canonical({"replay": replay_run_id, "cfg": cfg.config_hash}).encode()).hexdigest()[:20]


class BacktestEngine:
    def __init__(self, cfg: BacktestConfig) -> None:
        self.cfg = cfg

    # ------------------------------------------------------------------ signals
    def signals(self, replay: ReplayResult) -> tuple[list[_Signal], dict[str, int]]:
        cfg, out, skipped = self.cfg, [], {}
        for o in replay.observations:
            s = o.setup
            t = s.setup_time if cfg.entry_on == "candidate" else s.confirmed_at
            why = None
            if s.setup_type not in cfg.setup_types:
                why = "type_not_in_rule"
            elif t is None:
                why = "not_confirmed"
            elif RegimeFit(o.first_regime_fit) not in cfg.regime_fit_allowed:
                why = "regime_fit_not_allowed"
            if why:
                skipped[why] = skipped.get(why, 0) + 1
                continue
            out.append(_Signal(s, t, s.symbol))
        out.sort(key=lambda x: (x.decision_time, x.symbol, x.obs_setup.setup_id))
        return out, skipped

    @staticmethod
    def invalidation_reference(s) -> float:
        ev = dict(s.evidence)
        bull = s.direction is StructureDirection.BULLISH
        if s.setup_type is SetupType.SWEEP_REVERSAL:
            return float(ev["sweep_extreme"])
        if s.setup_type is SetupType.PULLBACK:
            return float(ev["level"]) - float(ev["zone"]) if bull else float(ev["level"]) + float(ev["zone"])
        return float(s.key_level)             # breakout: broken level; range rejection: boundary

    # ---------------------------------------------------------------------- run
    def run(self, replay: ReplayResult, candles_by_symbol: dict[str, Sequence[Candle]], tf: Timeframe,
            funding_by_symbol: dict[str, Sequence[FundingRate]]) -> BacktestResult:
        cfg, costs = self.cfg, self.cfg.costs
        run_id = backtest_run_id(replay.run_id, cfg)
        sigs, skipped = self.signals(replay)
        by_open: dict[tuple[str, datetime], list[_Signal]] = {}
        for s in sigs:
            by_open.setdefault((s.symbol, s.decision_time), []).append(s)   # entry bar opens at decision time
        timeline = sorted({c.open_time for cs in candles_by_symbol.values() for c in cs})
        bar_at = {sym: {c.open_time: (k, c) for k, c in enumerate(cs)} for sym, cs in candles_by_symbol.items()}
        known_bars = {sym: {c.open_time for c in cs} for sym, cs in candles_by_symbol.items()}
        for s in sigs:
            if s.decision_time not in known_bars.get(s.symbol, set()):
                skipped["no_next_bar"] = skipped.get("no_next_bar", 0) + 1
        open_pos: dict[str, _Open] = {}
        trades: list[SimulatedTrade] = []
        realized = Decimal(0)
        equity: list[tuple[datetime, float]] = []
        res = BacktestResult(run_id, replay.run_id, replay.dataset_id, cfg.config_hash, trades, equity, skipped)
        last_close: dict[str, Candle] = {}
        for t in timeline:
            for sym in sorted(candles_by_symbol):
                hit = bar_at[sym].get(t)
                if hit is None:
                    continue
                k, bar = hit
                res.bars += 1
                # 1. entries decided at the previous close
                for sig in by_open.get((sym, t), []):
                    if sym in open_pos:
                        skipped["position_already_open"] = skipped.get("position_already_open", 0) + 1
                        continue
                    pos = self._enter(sig, bar, k, candles_by_symbol[sym], len(trades) + len(open_pos) + 1, run_id,
                                      skipped)
                    if pos is not None:
                        open_pos[sym] = pos
                # 2. exits on this bar
                pos = open_pos.get(sym)
                if pos is not None:
                    pos.bars += 1
                    res.position_bars += 1
                    done = self._exit_check(pos, bar)
                    if done is None and pos.bars >= cfg.max_hold_bars:
                        done = (SimExitReason.TIME, self._market_exit(pos, bar), True, bar.close_time, _d(bar.close))
                    if done is not None:
                        trades.append(self._close(pos, done, run_id, replay.dataset_id, funding_by_symbol.get(sym, ()),
                                                  candles_by_symbol[sym], res))
                        realized += _d(trades[-1].net_pnl)
                        del open_pos[sym]
                last_close[sym] = bar
            unreal = sum((self._mtm(p, last_close[s]) for s, p in open_pos.items()), Decimal(0))
            equity.append((t + tf.delta, float(_d(cfg.initial_equity_quote) + realized + unreal)))
        for sym, pos in sorted(open_pos.items()):          # still open at the end of the data
            bar = last_close[sym]
            trades.append(self._close(pos, (SimExitReason.END_OF_DATA, self._market_exit(pos, bar), True,
                                            bar.close_time, _d(bar.close)), run_id, replay.dataset_id, funding_by_symbol.get(sym, ()),
                                      candles_by_symbol[sym], res))
            realized += _d(trades[-1].net_pnl)
        if open_pos and equity:
            equity[-1] = (equity[-1][0], float(_d(cfg.initial_equity_quote) + realized))
        trades.sort(key=lambda x: x.trade_no)
        return res

    # ------------------------------------------------------------------ pieces
    def _enter(self, sig: _Signal, bar: Candle, k: int, cs, n: int, run_id: str, skipped) -> _Open | None:
        cfg, costs = self.cfg, self.cfg.costs
        s = sig.obs_setup
        long = s.direction is StructureDirection.BULLISH
        side = PositionSide.LONG if long else PositionSide.SHORT
        atr = dict(s.evidence).get("atr")
        if not atr:
            skipped["no_atr"] = skipped.get("no_atr", 0) + 1
            return None
        ref = _d(cs[k - 1].close) if k > 0 else _d(bar.open)       # decision-time reference (known at decision)
        buf = _d(cfg.stop_buffer_atr) * _d(atr)
        inval = _d(self.invalidation_reference(s))
        stop = inval - buf if long else inval + buf
        if (long and stop >= ref) or (not long and stop <= ref):
            skipped["stop_on_wrong_side"] = skipped.get("stop_on_wrong_side", 0) + 1
            return None
        qty = _d(cfg.risk_per_trade_quote) / abs(ref - stop)
        slip = _d(costs.slippage_bps) / _BPS
        fill = _d(bar.open) * (1 + slip if long else 1 - slip)
        if (long and fill <= stop) or (not long and fill >= stop):
            skipped["opened_beyond_stop"] = skipped.get("opened_beyond_stop", 0) + 1
            return None
        target = fill + (fill - stop) * _d(cfg.take_profit_r) if long else fill - (stop - fill) * _d(cfg.take_profit_r)
        plan = RiskPlan(f"rsk_{run_id}_{n}", s.setup_id, side, ref, stop, qty)
        trade = Trade(f"sim_{run_id}_{n}", Symbol(s.symbol), side, plan, signal_id=s.setup_id)
        fee = fill * qty * _d(costs.taker_fee_bps) / _BPS
        f = Fill(f"sim-{run_id}-{n}-in", Symbol(s.symbol), side.entry_side, qty, fill, fee, "USDT", False,
                 bar.open_time, EventSource.SIMULATOR, expected_price=_d(bar.open))
        trade = trade.apply_fill(f, FillRole.ENTRY)
        return _Open(sig, trade, side, qty, fill, stop, target, bar.open_time, k)

    def _exit_check(self, pos: _Open, bar: Candle):
        """Returns (reason, price, is_taker, time, expected_price) or None."""
        long = pos.side is PositionSide.LONG
        slip = _d(self.cfg.costs.slippage_bps) / _BPS
        o, h, l = _d(bar.open), _d(bar.high), _d(bar.low)
        t = bar.close_time
        adverse = (lambda p: p * (1 - slip)) if long else (lambda p: p * (1 + slip))
        first_bar = bar.open_time == pos.entry_time
        if not first_bar:                                  # gaps at the open (the entry bar opened at the fill)
            if (long and o <= pos.stop) or (not long and o >= pos.stop):
                return SimExitReason.STOP, adverse(o), True, t, o
            if (long and o >= pos.target) or (not long and o <= pos.target):
                return SimExitReason.TAKE_PROFIT, pos.target, False, t, pos.target
        stop_hit = l <= pos.stop if long else h >= pos.stop
        tp_hit = h >= pos.target if long else l <= pos.target
        if stop_hit and tp_hit:
            pos.ambiguous = True
            return SimExitReason.STOP_AMBIGUOUS, adverse(pos.stop), True, t, pos.stop
        if stop_hit:
            return SimExitReason.STOP, adverse(pos.stop), True, t, pos.stop
        if tp_hit:
            return SimExitReason.TAKE_PROFIT, pos.target, False, t, pos.target
        return None

    def _market_exit(self, pos: _Open, bar: Candle) -> Decimal:
        """Market exit at the close: adverse slippage like every market fill."""
        slip = _d(self.cfg.costs.slippage_bps) / _BPS
        return _d(bar.close) * (1 - slip if pos.side is PositionSide.LONG else 1 + slip)

    def _mtm(self, pos: _Open, bar: Candle) -> Decimal:
        sign = 1 if pos.side is PositionSide.LONG else -1
        return (_d(bar.close) - pos.entry) * pos.qty * sign - pos.trade.pnl().fees

    def _funding(self, pos: _Open, exit_time: datetime, rates: Sequence[FundingRate], cs) -> tuple[list, str]:
        """Funding settles at funding times strictly after entry and at/before exit. Long pays a
        positive rate. Notional uses the close of the bar ending at the funding time (entry price if absent)."""
        mode = self.cfg.costs.funding_mode
        if mode == "none":
            return [], "none"
        closes = {c.close_time: _d(c.close) for c in cs}
        inside = [r for r in rates if pos.entry_time < r.funding_time <= exit_time]
        source = "data"
        if mode == "assumed" or (mode == "data_or_assumed" and not rates):
            source = "assumed"
            t0 = pos.entry_time.replace(minute=0, second=0, microsecond=0)
            ts, t = [], t0
            while t <= exit_time:
                if t > pos.entry_time and t.hour in _FUNDING_HOURS:
                    ts.append(t)
                t += timedelta(hours=1)
            inside = [FundingRate(Symbol(pos.sig.symbol), t, _d(self.cfg.costs.assumed_funding_rate_8h)) for t in ts]
        sign = 1 if pos.side is PositionSide.LONG else -1
        evs = []
        for r in inside:
            notional = closes.get(r.funding_time, pos.entry) * pos.qty
            evs.append(FundingEvent(f"fnd_{pos.trade.trade_id}_{r.funding_time.isoformat()}", Symbol(pos.sig.symbol),
                                    -sign * _d(r.rate) * notional, "USDT", r.funding_time,
                                    f"{source}:{r.funding_time.isoformat()}", _d(r.rate), pos.trade.trade_id))
        return evs, source

    def _close(self, pos: _Open, done, run_id: str, dataset_id: str, rates, cs, res: BacktestResult) -> SimulatedTrade:
        reason, price, taker, t, expected = done
        costs = self.cfg.costs
        bps = costs.taker_fee_bps if taker else costs.maker_fee_bps
        fee = price * pos.qty * _d(bps) / _BPS
        n = int(pos.trade.trade_id.rsplit("_", 1)[1])
        f = Fill(f"sim-{run_id}-{n}-out", pos.trade.symbol, pos.side.entry_side.opposite, pos.qty, price, fee, "USDT",
                 not taker, t, EventSource.SIMULATOR, expected_price=expected)
        tr = pos.trade.apply_fill(f, FillRole.EXIT)
        evs, source = self._funding(pos, t, rates, cs)
        res.funding_sources[source] = res.funding_sources.get(source, 0) + 1
        for ev in evs:
            tr = tr.apply_funding(ev)
        p = tr.pnl()
        r = tr.realized_r()
        s = pos.sig.obs_setup
        return SimulatedTrade(
            run_id, n, dataset_id, s.symbol, s.setup_id, s.setup_type.value, s.direction.value,
            pos.sig.decision_time, pos.entry_time, float(pos.entry), float(pos.qty), float(pos.stop), float(pos.target),
            t, float(price), reason, pos.bars, float(p.gross), float(p.fees), float(p.funding), float(p.slippage_cost),
            float(p.net), float(tr.initial_risk_amount), float(r) if r is not None else None, source, pos.ambiguous)


# ============================================================ Phase 9 decision path
# The v1 path above is untouched. DecisionBacktestEngine reuses its fill / exit / funding /
# accounting methods unchanged; only WHO decides entry and size differs (the DecisionGate).
_EXECUTION_SKIPS = ("no_next_bar", "position_already_open", "opened_beyond_stop")


@dataclass
class DecisionBacktestResult(BacktestResult):
    decisions: list[dict] = field(default_factory=list)
    halted_at: datetime | None = None


def decision_run_id(replay_run_id: str, gate_config: dict, run_config_hash: str) -> str:
    return "bt_" + hashlib.sha256(canonical({"replay": replay_run_id, "gate": gate_config,
                                             "run": run_config_hash}).encode()).hexdigest()[:20]


class DecisionBacktestEngine(BacktestEngine):
    """Event-driven backtest where a DecisionGate (Strategy -> Risk) decides entry and size.
    self.cfg is a DecisionRunConfig (costs, initial equity) - duck-typed for the inherited
    execution helpers, which only read cfg.costs."""

    def run_gate(self, replay: ReplayResult, candles_by_symbol: dict[str, Sequence[Candle]], tf: Timeframe,
                 funding_by_symbol: dict[str, Sequence[FundingRate]], gate, instruments: dict,
                 run_config_hash: str) -> DecisionBacktestResult:
        from app.domain.decision import OpenExposure, PortfolioState, setup_view
        from app.domain.enums import RiskStatus
        cfg = self.cfg
        run_id = decision_run_id(replay.run_id, dict(gate.config), run_config_hash)
        all_setups = [o.setup for o in replay.observations]
        fit = {o.setup.setup_id: o.first_regime_fit for o in replay.observations}
        skipped: dict[str, int] = {}
        pending: dict[datetime, list] = {}
        for o in replay.observations:
            t = o.setup.setup_time if gate.entry_on == "candidate" else o.setup.confirmed_at
            if t is None:
                skipped["not_decidable"] = skipped.get("not_decidable", 0) + 1
                continue
            pending.setdefault(t, []).append(o.setup)
        timeline = sorted({c.open_time for cs in candles_by_symbol.values() for c in cs})
        bar_at = {sym: {c.open_time: (k, c) for k, c in enumerate(cs)} for sym, cs in candles_by_symbol.items()}
        known = {sym: {c.open_time for c in cs} for sym, cs in candles_by_symbol.items()}
        res = DecisionBacktestResult(run_id, replay.run_id, replay.dataset_id, run_config_hash, [], [], skipped)
        trades, equity, decisions = res.trades, res.equity, res.decisions
        open_pos: dict[str, _Open] = {}
        hold: dict[str, int] = {}
        realized = Decimal(0)
        last_close: dict[str, Candle] = {}
        init = _d(cfg.initial_equity_quote)
        peak, halted, kill = init, False, False

        def bump(key):
            skipped[key] = skipped.get(key, 0) + 1

        def equity_now() -> Decimal:
            return _d(equity[-1][1]) if equity else init

        def day_start_equity(t: datetime) -> Decimal:
            day = t.replace(hour=0, minute=0, second=0, microsecond=0)
            before = [v for a, v in equity if a <= day]
            return _d(before[-1]) if before else init

        def realized_today(t: datetime) -> Decimal:
            day = t.replace(hour=0, minute=0, second=0, microsecond=0)
            return sum((_d(x.net_pnl) for x in trades if day <= x.exit_time <= t), Decimal(0))

        for t in timeline:
            # 1. decisions for facts known at the previous close (= open of t), all on the same state
            for s in sorted(pending.pop(t, []), key=lambda x: (x.symbol, x.setup_id)):
                rec = {"setup_id": s.setup_id, "symbol": s.symbol, "decision_time": t.isoformat()}
                if t not in known.get(s.symbol, set()):
                    bump("no_next_bar")
                    decisions.append({**rec, "outcome": "no_next_bar"})
                    continue
                if s.symbol in open_pos:
                    bump("position_already_open")
                    decisions.append({**rec, "outcome": "position_already_open"})
                    continue
                k, bar = bar_at[s.symbol][t]
                ref = _d(candles_by_symbol[s.symbol][k - 1].close) if k > 0 else _d(bar.open)
                eq = equity_now()
                p = PortfolioState(t, eq, max(peak, eq), day_start_equity(t), realized_today(t),
                                   tuple(OpenExposure(sym, pos.side, pos.trade.initial_risk_amount, pos.qty * pos.entry)
                                         for sym, pos in sorted(open_pos.items())), halted, kill)
                view = setup_view(s, t, fit[s.setup_id], all_setups)
                g = gate.evaluate(view, ref, p, instruments.get(s.symbol))
                rec.update({"strategy": {"reason_code": g.strategy.reason_code, "detail": g.strategy.reason_detail,
                                         "decision_id": g.strategy.decision_id}})
                if g.risk is not None:
                    rec["risk"] = {"status": g.risk.status.value, "reason_code": g.risk.reason_code,
                                   "reason_codes": list(g.risk.reason_codes), "qty": str(g.risk.qty),
                                   "decision_id": g.risk.decision_id,
                                   "checks": [[c.name, c.passed, c.value, c.limit] for c in g.risk.checks],
                                   "instrument_diagnostic": dict(g.risk.instrument_diagnostic)}
                    halted = halted or g.risk.status is RiskStatus.HALTED
                    kill = kill or g.risk.reason_code == "risk:kill_switch_active"
                if not g.enter:
                    bump(g.reason_code)
                    decisions.append({**rec, "outcome": g.reason_code})
                    if halted and res.halted_at is None:
                        res.halted_at = t
                    continue
                pos = self._enter_planned(s, g, bar, t, len(trades) + len(open_pos) + 1, run_id)
                if pos is None:
                    bump("opened_beyond_stop")
                    decisions.append({**rec, "outcome": "opened_beyond_stop"})
                    continue
                open_pos[s.symbol], hold[s.symbol] = pos, g.strategy.max_hold_bars
                decisions.append({**rec, "outcome": "entered", "trade_no": int(pos.trade.trade_id.rsplit("_", 1)[1])})
            # 2. exits - identical to the v1 loop
            for sym in sorted(candles_by_symbol):
                hit = bar_at[sym].get(t)
                if hit is None:
                    continue
                k, bar = hit
                res.bars += 1
                pos = open_pos.get(sym)
                if pos is not None:
                    pos.bars += 1
                    res.position_bars += 1
                    done = self._exit_check(pos, bar)
                    if done is None and pos.bars >= hold[sym]:
                        done = (SimExitReason.TIME, self._market_exit(pos, bar), True, bar.close_time, _d(bar.close))
                    if done is not None:
                        trades.append(self._close(pos, done, run_id, replay.dataset_id,
                                                  funding_by_symbol.get(sym, ()), candles_by_symbol[sym], res))
                        realized += _d(trades[-1].net_pnl)
                        del open_pos[sym]
                last_close[sym] = bar
            unreal = sum((self._mtm(p_, last_close[s_]) for s_, p_ in open_pos.items()), Decimal(0))
            equity.append((t + tf.delta, float(init + realized + unreal)))
            peak = max(peak, _d(equity[-1][1]))
        for sym, pos in sorted(open_pos.items()):
            bar = last_close[sym]
            trades.append(self._close(pos, (SimExitReason.END_OF_DATA, self._market_exit(pos, bar), True,
                                            bar.close_time, _d(bar.close)), run_id, replay.dataset_id,
                                      funding_by_symbol.get(sym, ()), candles_by_symbol[sym], res))
            realized += _d(trades[-1].net_pnl)
        if open_pos and equity:
            equity[-1] = (equity[-1][0], float(init + realized))
        for leftover in pending.values():                     # decision times with no bar at all in the data
            for s in leftover:
                bump("no_next_bar")
                decisions.append({"setup_id": s.setup_id, "symbol": s.symbol, "outcome": "no_next_bar"})
        trades.sort(key=lambda x: x.trade_no)
        return res

    def _enter_planned(self, s, g, bar: Candle, t: datetime, n: int, run_id: str) -> _Open | None:
        """Market entry at the open of the bar after the decision with the RiskPlan of the
        risk decision (qty, frozen stop); fill / fee exactly as the v1 path."""
        costs = self.cfg.costs
        plan = g.risk.risk_plan
        long = plan.side is PositionSide.LONG
        slip = _d(costs.slippage_bps) / _BPS
        fill = _d(bar.open) * (1 + slip if long else 1 - slip)
        stop = plan.initial_stop
        if (long and fill <= stop) or (not long and fill >= stop):
            return None
        tp_r = g.strategy.take_profit_r
        target = fill + (fill - stop) * tp_r if long else fill - (stop - fill) * tp_r
        trade = Trade(f"sim_{run_id}_{n}", Symbol(s.symbol), plan.side, plan, signal_id=s.setup_id)
        fee = fill * plan.planned_qty * _d(costs.taker_fee_bps) / _BPS
        f = Fill(f"sim-{run_id}-{n}-in", Symbol(s.symbol), plan.side.entry_side, plan.planned_qty, fill, fee, "USDT",
                 False, bar.open_time, EventSource.SIMULATOR, expected_price=_d(bar.open))
        trade = trade.apply_fill(f, FillRole.ENTRY)
        return _Open(_Signal(s, t, s.symbol), trade, plan.side, plan.planned_qty, fill, stop, target, bar.open_time,
                     0)
