"""Setup engine v1: one symbol, one timeframe. Pure, point-in-time.

Single chronological pass over the last `lookback_bars` closed bars. At bar j:
  1. open setups are updated with bar j   (invalidation -> confirmation -> expiry)
  2. pullback "arms" (BOS levels waiting for a return) are updated with bar j
  3. new triggers are read from the structure KNOWN AT the close of bar j
     (structure_at(close_j): only events whose event_time == close_j are new);
     range rejection uses the structure known BEFORE bar j opened (close_{j-1}).
Nothing is read from storage, so the result at as_of is a function of data <= as_of.
Definitions: docs/V070_PHASE6_SETUP_DETECTION.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import app
from app.domain.enums import (LiquiditySide, QualityStatus, RegimeFit, SetupStatus, SetupType, StructureDirection,
                              StructureEventType)
from app.domain.regime import RegimeSnapshot
from app.domain.research import Feature
from app.domain.setup import Setup, SetupSnapshot, SetupTransition
from app.domain.structure import StructureSnapshot
from app.research.engine import input_fingerprint
from app.research.quality import DataQualityPolicy, ValidatedSeries
from app.research.setup.config import SetupConfig
from app.research.structure.engine import stable_id

SETUP_ENGINE_VERSION = "1"

BULL, BEAR = StructureDirection.BULLISH, StructureDirection.BEARISH
_CONTINUATION = {SetupType.BREAKOUT, SetupType.PULLBACK}
_REGIME_TREND = {"up": BULL, "down": BEAR}

StructureAt = Callable[[datetime], StructureSnapshot]
AtrAt = Callable[[datetime], Feature]


@dataclass
class _Draft:
    """Mutable lifecycle of one setup while replaying bars."""
    setup_id: str
    type: SetupType
    direction: StructureDirection
    trigger_id: str
    trigger_time: datetime
    level: float
    created_idx: int
    invalid_rule: str
    evidence: dict[str, Any]
    params: dict[str, float]                  # rule inputs frozen at creation
    level_key: str = ""                       # for de-duplication (range rejection)
    transitions: list[SetupTransition] = field(default_factory=list)
    holds: int = 0

    @property
    def status(self) -> SetupStatus:
        return self.transitions[-1].status

    @property
    def open(self) -> bool:
        return self.status not in (SetupStatus.INVALIDATED, SetupStatus.EXPIRED)


@dataclass
class _Arm:
    """A BOS level waiting for price to come back (pullback)."""
    event_id: str
    direction: StructureDirection
    level: float
    zone: float
    atr: float
    created_idx: int
    event_time: datetime
    departed: bool = False      # a close beyond level +/- zone happened (price moved AWAY first)


class SetupEngine:
    def __init__(self, config: SetupConfig, structure_min_bars: int,
                 policy: DataQualityPolicy = DataQualityPolicy()) -> None:
        self.cfg = config
        self.structure_min_bars = structure_min_bars
        self.policy = policy

    # ================================================================== public
    def required_bars(self) -> int:
        """Contiguous closed bars needed: structure at the oldest replayed bar + the replay."""
        return self.structure_min_bars + self.cfg.lookback_bars + 1

    def evaluate(self, series: ValidatedSeries, structure_at: StructureAt, atr_at: AtrAt,
                 regime: RegimeSnapshot | None, evaluated_at: datetime) -> SetupSnapshot:
        cfg, tf, sym = self.cfg, series.timeframe, series.symbol
        regime_ctx = _regime_context(regime)
        if tf not in cfg.timeframes:
            return self._empty(series, QualityStatus.VALID, (f"TIMEFRAME_NOT_ENABLED:{tf.value}",), regime_ctx, None,
                               evaluated_at)
        a = self.policy.assess(series, self.required_bars())
        if not a.ok:
            return self._empty(series, a.quality, (f"SETUP_UNAVAILABLE:{tf.value}:{a.quality.value}",
                                                   f"REASON:{a.reason}"), regime_ctx, None, evaluated_at)
        n = len(series)
        s = n - 1 - cfg.lookback_bars
        t, o, h, l, c = series.times, series.o, series.h, series.l, series.c
        close_t = lambda i: t[i] + tf.delta  # noqa: E731
        drafts: list[_Draft] = []
        arms: list[_Arm] = []
        codes: list[str] = []
        struct_cache: dict[int, StructureSnapshot] = {}

        def st(i: int) -> StructureSnapshot:
            if i not in struct_cache:
                struct_cache[i] = structure_at(close_t(i))
            return struct_cache[i]

        for j in range(s, n):
            at = close_t(j)
            # 1. lifecycle of setups created on earlier bars
            for d in drafts:
                if d.open and d.created_idx < j:
                    self._step(d, j, at, c, st)
            # 2. pullback arms
            for arm in list(arms):
                made = self._arm_step(arm, j, at, h, l, c, sym.name, tf)
                if made is not None:
                    drafts.append(made)
                    arms.remove(arm)
                elif made is None and (j - arm.created_idx > cfg.ttl_bars or self._arm_lost(arm, c[j])):
                    arms.remove(arm)
            # 3. new triggers known at the close of bar j
            now_st = st(j)
            if now_st.data_quality is not QualityStatus.VALID:
                codes.append(f"STRUCTURE_UNAVAILABLE_AT:{at.isoformat()}")
                continue
            atr = atr_at(at)
            if not atr.is_valid:
                codes.append(f"ATR_UNAVAILABLE_AT:{at.isoformat()}")
                continue
            a_v = float(atr.value)  # type: ignore[arg-type]
            for e in now_st.events:
                if e.event_time != at or e.event_type is not StructureEventType.BOS:
                    continue              # BREAK_UNCLASSIFIED / CHoCH are not continuation triggers
                if SetupType.BREAKOUT in cfg.enabled_types:
                    drafts.append(self._breakout(e, j, at, c[j], a_v, sym.name, tf))
                if SetupType.PULLBACK in cfg.enabled_types:
                    z = cfg.pullback_zone_atr * a_v
                    departed = c[j] > e.level_price + z if e.direction is BULL else c[j] < e.level_price - z
                    arms.append(_Arm(e.event_id, e.direction, e.level_price, z, a_v, j, at, departed))
            if SetupType.SWEEP_REVERSAL in cfg.enabled_types:
                for w in now_st.sweeps:
                    if w.event_time == at:
                        drafts.append(self._sweep(w, j, at, h[j], l[j], a_v, sym.name, tf))
            if SetupType.RANGE_REJECTION in cfg.enabled_types and j > 0:
                drafts += self._range(j, at, h, l, c, st(j - 1), atr_at(close_t(j - 1)), drafts, sym.name, tf)

        horizon = n - 1 - (cfg.ttl_bars + cfg.recent_closed_bars)
        shown = [d for d in drafts if d.created_idx >= horizon]
        final_st = st(n - 1)
        ordered = sorted(shown, key=lambda d: (d.created_idx, d.setup_id))
        setups = tuple(self._freeze(d, regime, final_st, ordered) for d in ordered)
        codes = [f"SETUPS:{len(setups)}", f"ACTIVE:{sum(1 for x in setups if x.is_active)}",
                 f"LOOKBACK_BARS:{cfg.lookback_bars}"] + sorted(set(codes))
        if regime is None:
            codes.append("REGIME_CONTEXT_UNAVAILABLE")
        if {x.direction for x in setups if x.is_active} == {BULL, BEAR}:
            codes.append("CONFLICTING_ACTIVE_SETUPS")
        fp = input_fingerprint([(series, n)])
        if regime is not None:
            fp = stable_id(fp, regime.input_fingerprint)
        return SetupSnapshot(sym, tf, series.as_of, evaluated_at, QualityStatus.VALID, tuple(codes), setups,
                             regime_ctx, _structure_context(final_st), app.__version__, SETUP_ENGINE_VERSION,
                             cfg.config_hash, fp)

    # =============================================================== triggers
    def _new(self, typ, direction, trigger_id, trigger_time, level, j, at, rule, evidence, params, name, tf,
             level_key="") -> _Draft:
        sid = stable_id(name, tf.value, typ.value, direction.value, trigger_id, SETUP_ENGINE_VERSION,
                        self.cfg.config_hash)
        d = _Draft(sid, typ, direction, trigger_id, trigger_time, level, j, rule, evidence, params, level_key)
        d.transitions.append(SetupTransition(SetupStatus.CANDIDATE, at, f"TRIGGER:{typ.value.upper()}"))
        return d

    def _breakout(self, e, j, at, close, atr, name, tf) -> _Draft:
        bull = e.direction is BULL
        rule = f"close {'<' if bull else '>'} {e.level_price}"
        ev = {"atr": atr, "bos_event_time": e.event_time.isoformat(), "break_close": close,
              "break_distance_atr": abs(close - e.level_price) / atr if atr else None, "level": e.level_price,
              "swing_pivot_time": e.swing_pivot_time.isoformat()}
        return self._new(SetupType.BREAKOUT, e.direction, e.event_id, e.event_time, e.level_price, j, at, rule, ev,
                         {}, name, tf)

    def _arm_step(self, arm: _Arm, j, at, h, l, c, name, tf) -> _Draft | None:
        """A pullback needs price to move AWAY from the broken level first (a close beyond
        level +/- zone, on the BOS bar or later) and THEN come back into the zone on a later
        bar without losing it. Without the departure, the bar after a BOS would 'touch' the
        level automatically. Approach alone is a CANDIDATE, never a confirmation."""
        if j <= arm.created_idx or j - arm.created_idx > self.cfg.ttl_bars:
            return None
        L, z = arm.level, arm.zone
        bull = arm.direction is BULL
        if arm.departed:
            if bull:
                touched, held = l[j] <= L + z, c[j] >= L - z
            else:
                touched, held = h[j] >= L - z, c[j] <= L + z
        else:
            touched = held = False
        if not arm.departed and (c[j] > L + z if bull else c[j] < L - z):
            arm.departed = True                 # departure seen at the close of bar j; returns count from j+1
        if not (touched and held):
            return None
        bull = arm.direction is BULL
        rule = f"close {'<' if bull else '>'} {L - z if bull else L + z} (level -/+ zone)"
        ev = {"atr": arm.atr, "bos_event_time": arm.event_time.isoformat(), "level": L, "touch_bar_high": h[j],
              "touch_bar_low": l[j], "zone": z}
        return self._new(SetupType.PULLBACK, arm.direction, arm.event_id, arm.event_time, L, j, at, rule, ev,
                         {"zone": z, "touch_high": h[j], "touch_low": l[j]}, name, tf)

    @staticmethod
    def _arm_lost(arm: _Arm, close: float) -> bool:
        return close < arm.level - arm.zone if arm.direction is BULL else close > arm.level + arm.zone

    def _sweep(self, w, j, at, hi, lo, atr, name, tf) -> _Draft:
        # buy-side liquidity swept -> potential bearish reversal; sell-side -> bullish
        direction = BEAR if w.side is LiquiditySide.BUY_SIDE else BULL
        rule = f"close {'>' if direction is BEAR else '<'} {w.extreme} (beyond the sweep extreme)"
        ev = {"atr": atr, "level": w.level_price, "sweep_bar_close": w.close, "sweep_bar_high": hi,
              "sweep_bar_low": lo, "sweep_extreme": w.extreme, "swept_level_id": w.level_id}
        return self._new(SetupType.SWEEP_REVERSAL, direction, w.event_id, w.event_time, w.level_price, j, at, rule, ev,
                         {"extreme": w.extreme, "bar_high": hi, "bar_low": lo}, name, tf)

    def _range(self, j, at, h, l, c, prev: StructureSnapshot, atr_prev: Feature, drafts, name, tf) -> list[_Draft]:
        """Range = nearest ACTIVE buy-side level above and sell-side level below the previous
        close, as known before bar j opened. Rejection = bar j trades into the touch zone of a
        boundary WITHOUT piercing it and closes in the opposite part of its own range."""
        cfg = self.cfg
        if prev.data_quality is not QualityStatus.VALID or not atr_prev.is_valid:
            return []
        a = float(atr_prev.value)  # type: ignore[arg-type]
        ref = c[j - 1]
        tops = [lv for lv in prev.liquidity if lv.side is LiquiditySide.BUY_SIDE and lv.price >= ref]
        bots = [lv for lv in prev.liquidity if lv.side is LiquiditySide.SELL_SIDE and lv.price <= ref]
        if not tops or not bots or h[j] == l[j]:
            return []
        top = min(tops, key=lambda x: (x.price, x.level_id))
        bot = max(bots, key=lambda x: (x.price, x.level_id))
        width = top.price - bot.price
        if not cfg.range_min_width_atr * a <= width <= cfg.range_max_width_atr * a:
            return []
        out = []
        span = h[j] - l[j]
        touch = cfg.range_touch_atr * a
        base_ev = {"atr": a, "range_bottom": bot.price, "range_top": top.price, "width_atr": width / a,
                   "bar_high": h[j], "bar_low": l[j], "bar_close": c[j]}
        # bearish rejection of the top
        if top.price - touch <= h[j] <= top.price and c[j] <= l[j] + cfg.rejection_close_fraction * span \
                and not self._open_same(drafts, top.level_id, BEAR):
            out.append(self._new(SetupType.RANGE_REJECTION, BEAR, stable_id(top.level_id, at.isoformat()), at,
                                 top.price, j, at, f"close > {top.price} (range top)",
                                 {**base_ev, "boundary": "top", "level_id": top.level_id},
                                 {"bar_high": h[j], "bar_low": l[j]}, name, tf, level_key=top.level_id))
        if bot.price <= l[j] <= bot.price + touch and c[j] >= h[j] - cfg.rejection_close_fraction * span \
                and not self._open_same(drafts, bot.level_id, BULL):
            out.append(self._new(SetupType.RANGE_REJECTION, BULL, stable_id(bot.level_id, at.isoformat()), at,
                                 bot.price, j, at, f"close < {bot.price} (range bottom)",
                                 {**base_ev, "boundary": "bottom", "level_id": bot.level_id},
                                 {"bar_high": h[j], "bar_low": l[j]}, name, tf, level_key=bot.level_id))
        return out

    @staticmethod
    def _open_same(drafts: list[_Draft], level_key: str, direction) -> bool:
        return any(d.open and d.type is SetupType.RANGE_REJECTION and d.level_key == level_key
                   and d.direction is direction for d in drafts)

    # ============================================================== lifecycle
    def _step(self, d: _Draft, j: int, at: datetime, c, st) -> None:
        """Order inside a bar: invalidation, then confirmation, then expiry."""
        cfg, bull, cj = self.cfg, d.direction is BULL, c[j]
        p = d.params
        if d.type is SetupType.BREAKOUT:
            lost = cj < d.level if bull else cj > d.level
            if lost:
                return self._to(d, SetupStatus.INVALIDATED, at, "CLOSE_BACK_INSIDE_BROKEN_LEVEL")
            if d.status is SetupStatus.CANDIDATE:
                d.holds += 1
                if d.holds >= cfg.breakout_hold_bars:
                    self._to(d, SetupStatus.CONFIRMED, at, f"HELD_BEYOND_LEVEL_{cfg.breakout_hold_bars}_CLOSES")
        elif d.type is SetupType.PULLBACK:
            lost = cj < d.level - p["zone"] if bull else cj > d.level + p["zone"]
            if lost:
                return self._to(d, SetupStatus.INVALIDATED, at, "LEVEL_LOST")
            if d.status is SetupStatus.CANDIDATE and (cj > p["touch_high"] if bull else cj < p["touch_low"]):
                self._to(d, SetupStatus.CONFIRMED, at, "CLOSE_BEYOND_TOUCH_BAR")
        elif d.type is SetupType.SWEEP_REVERSAL:
            accepted = cj > p["extreme"] if d.direction is BEAR else cj < p["extreme"]
            if accepted:
                return self._to(d, SetupStatus.INVALIDATED, at, "CLOSE_BEYOND_SWEEP_EXTREME")
            if d.status is SetupStatus.CANDIDATE:
                if cfg.sweep_confirmation == "close_beyond_sweep_bar":
                    if cj < p["bar_low"] if d.direction is BEAR else cj > p["bar_high"]:
                        self._to(d, SetupStatus.CONFIRMED, at, "CLOSE_BEYOND_SWEEP_BAR")
                else:
                    now = st(j)
                    if any(e.event_time == at and e.direction is d.direction for e in now.events):
                        self._to(d, SetupStatus.CONFIRMED, at, "OPPOSITE_STRUCTURE_BREAK")
        elif d.type is SetupType.RANGE_REJECTION:
            lost = cj > d.level if d.direction is BEAR else cj < d.level
            if lost:
                return self._to(d, SetupStatus.INVALIDATED, at, "CLOSE_BEYOND_RANGE_BOUNDARY")
            if d.status is SetupStatus.CANDIDATE and (cj < p["bar_low"] if d.direction is BEAR else cj > p["bar_high"]):
                self._to(d, SetupStatus.CONFIRMED, at, "CLOSE_BEYOND_REJECTION_BAR")
        if d.open and j - d.created_idx >= cfg.ttl_bars:
            self._to(d, SetupStatus.EXPIRED, at, "TTL_ELAPSED")

    @staticmethod
    def _to(d: _Draft, status: SetupStatus, at: datetime, reason: str) -> None:
        d.transitions.append(SetupTransition(status, at, reason))

    # =================================================================== output
    def _freeze(self, d: _Draft, regime: RegimeSnapshot | None, final_st: StructureSnapshot,
                shown: list[_Draft]) -> Setup:
        fit, contra = self._context(d, regime, final_st)
        if d.open:
            contra += sorted(f"OPPOSITE_SETUP_ACTIVE:{o.type.value}:{o.setup_id}" for o in shown
                             if o.open and o.direction is not d.direction)
        codes = tuple(f"{t.status.value.upper()}:{t.reason}" for t in d.transitions)
        ev = tuple(sorted((k, v) for k, v in d.evidence.items() if v is not None))
        return Setup(d.setup_id, final_st.symbol.name, final_st.timeframe, d.type, d.direction, d.trigger_id,
                     d.trigger_time, d.level, d.invalid_rule, tuple(d.transitions), codes, ev, tuple(contra), fit,
                     QualityStatus.VALID, SETUP_ENGINE_VERSION, self.cfg.config_hash)

    def _context(self, d: _Draft, regime: RegimeSnapshot | None, final_st: StructureSnapshot):
        contra: list[str] = []
        if regime is None or regime.data_quality is not QualityStatus.VALID or regime.regime.value == "unknown":
            fit = RegimeFit.UNKNOWN
        elif regime.regime in self.cfg.allowed_regimes[d.type]:
            fit = RegimeFit.ALLOWED
        else:
            fit = RegimeFit.NOT_ALLOWED
            contra.append(f"REGIME_NOT_ALLOWED:{regime.regime.value}")
        if d.type in _CONTINUATION:
            if regime is not None and regime.data_quality is QualityStatus.VALID:
                rdir = _REGIME_TREND.get(regime.trend_direction.value)
                if rdir is not None and rdir is not d.direction:
                    contra.append(f"REGIME_TREND_OPPOSES:{regime.trend_direction.value}")
            if final_st.direction not in (d.direction, StructureDirection.UNKNOWN):
                contra.append(f"STRUCTURE_DIRECTION_OPPOSES:{final_st.direction.value}")
        return fit, contra

    def _empty(self, series, q, codes, regime_ctx, structure_ctx, evaluated_at) -> SetupSnapshot:
        return SetupSnapshot(series.symbol, series.timeframe, series.as_of, evaluated_at, q, tuple(codes), (),
                             regime_ctx, structure_ctx, app.__version__, SETUP_ENGINE_VERSION, self.cfg.config_hash,
                             input_fingerprint([(series, len(series))]))


def _regime_context(r: RegimeSnapshot | None) -> dict | None:
    if r is None:
        return None
    return {"as_of": r.as_of.isoformat(), "regime": r.regime.value, "trend_direction": r.trend_direction.value,
            "volatility_state": r.volatility_state.value, "data_quality": r.data_quality.value,
            "timeframe": r.timeframe.value, "regime_version": r.regime_version, "config_hash": r.config_hash,
            "input_fingerprint": r.input_fingerprint}


def _structure_context(s: StructureSnapshot) -> dict:
    return {"as_of": s.as_of.isoformat(), "direction": s.direction.value, "data_quality": s.data_quality.value,
            "structure_version": s.structure_version, "config_hash": s.config_hash,
            "input_fingerprint": s.input_fingerprint}
