"""Structure engine v1: one symbol, one timeframe, closed bars only. Pure.

Bars are processed strictly in time order by a single state machine; at bar t
only information known when bar t OPENED (swings/levels confirmed at a close
<= open of t) plus bar t itself is used. Definitions: docs/V070_MARKET_STRUCTURE.md.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

import app
from app.domain.enums import (BreakMethod, LevelStatus, LiquiditySide, LiquiditySource, QualityStatus,
                              StructureDirection, StructureEventType, SwingKind)
from app.domain.research import Feature
from app.domain.structure import LiquidityLevel, StructureEvent, StructureSnapshot, Swing, SweepEvent
from app.research.engine import input_fingerprint
from app.research.quality import DataQualityPolicy, ValidatedSeries
from app.research.structure.config import StructureConfig

STRUCTURE_VERSION = "1"
SWING_ALGORITHM = "fractal-v1"
SWEEP_METHOD = "wick_through_close_back_same_bar"

AtrProvider = Callable[[datetime], Feature]


def stable_id(*parts: object) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:24]


# ------------------------------------------------------------------ swings
def detect_swings(h: list[float], l: list[float], left: int, right: int) -> tuple[list[int], list[int]]:
    """Fractal pivots. Swing high at i: h[i] STRICTLY above each of the `left`
    previous highs and >= each of the `right` next highs (lows mirrored).
    Tie rule: of equal extremes within `left` bars, the EARLIEST is the pivot.
    A pivot at i is confirmed by the close of bar i + right."""
    highs, lows = [], []
    for i in range(left, len(h) - right):
        hi, lo = h[i], l[i]
        if all(hi > h[j] for j in range(i - left, i)) and all(hi >= h[j] for j in range(i + 1, i + right + 1)):
            highs.append(i)
        if all(lo < l[j] for j in range(i - left, i)) and all(lo <= l[j] for j in range(i + 1, i + right + 1)):
            lows.append(i)
    return highs, lows


@dataclass
class _Level:
    level_id: str
    side: LiquiditySide
    source: LiquiditySource
    price: float
    zone_low: float
    zone_high: float
    members: list[tuple[int, datetime, float]]      # (bar index, pivot time, price)
    created_idx: int
    created_at: datetime
    updated_at: datetime
    status: LevelStatus = LevelStatus.ACTIVE
    status_reason: str = "created"
    tolerance: float | None = None
    atr: float | None = None
    ref_price: float = 0.0

    def freeze(self, sym: str, tf, cfg_hash: str) -> LiquidityLevel:
        method = "swing" if self.source in (LiquiditySource.SWING_HIGH, LiquiditySource.SWING_LOW) \
            else "equal_extremes_atr_tolerance"
        return LiquidityLevel(self.level_id, sym, tf, self.side, self.source, self.price, self.zone_low,
                              self.zone_high, tuple(m[1] for m in self.members), tuple(m[2] for m in self.members),
                              self.created_at, self.updated_at, self.status, self.status_reason, self.tolerance,
                              self.atr, method, QualityStatus.VALID, STRUCTURE_VERSION, cfg_hash)


@dataclass
class _State:
    trend: StructureDirection = StructureDirection.UNKNOWN
    trend_since: datetime | None = None
    last_high: tuple[int, int] | None = None     # (pivot idx, confirm idx) of the structure high
    last_low: tuple[int, int] | None = None
    high_broken: bool = False
    low_broken: bool = False
    events: list[StructureEvent] = field(default_factory=list)
    sweeps: list[SweepEvent] = field(default_factory=list)
    levels: list[_Level] = field(default_factory=list)
    swing_level: dict[tuple[SwingKind, int], _Level] = field(default_factory=dict)
    eq_unavailable: int = 0


class StructureEngine:
    def __init__(self, config: StructureConfig, policy: DataQualityPolicy = DataQualityPolicy()) -> None:
        self.cfg = config
        self.policy = policy

    # ================================================================ public
    def compute(self, series: ValidatedSeries, atr_at: AtrProvider) -> StructureSnapshot:
        cfg, tf, sym = self.cfg, series.timeframe, series.symbol
        a = self.policy.assess(series, cfg.min_bars)
        if not a.ok:
            return self._empty(series, a.quality, (f"STRUCTURE_UNAVAILABLE:{tf.value}:{a.quality.value}",
                                                   f"REASON:{a.reason}"))
        n = len(series)
        w = min(a.tail_bars, cfg.window_bars)
        s0 = n - w
        times, o, h, l, c = (series.times[s0:], series.o[s0:], series.h[s0:], series.l[s0:], series.c[s0:])
        step = tf.delta
        close_t = lambda i: times[i] + step  # noqa: E731
        L, R = cfg.swing_left, cfg.swing_right
        hi_idx, lo_idx = detect_swings(h, l, L, R)
        confirm: dict[int, list[tuple[SwingKind, int]]] = {}
        for i in hi_idx:
            confirm.setdefault(i + R, []).append((SwingKind.HIGH, i))
        for i in lo_idx:
            confirm.setdefault(i + R, []).append((SwingKind.LOW, i))
        st = _State()
        name, h_cfg = sym.name, cfg.config_hash
        atr_cache: dict[datetime, Feature] = {}

        def atr(t: datetime) -> Feature:
            if t not in atr_cache:
                atr_cache[t] = atr_at(t)
            return atr_cache[t]

        for t in range(w + 1):
            # 1. swings confirmed at the close of bar t-1 become known before bar t opens
            for kind, i in sorted(confirm.get(t - 1, []), key=lambda x: (x[0].value, x[1])):
                self._on_swing(st, kind, i, t - 1, times, h, l, close_t, atr, name, tf, h_cfg)
            if t == w:
                break
            # 2. liquidity interactions of bar t with levels known before it opened
            for lv in st.levels:
                if lv.status is LevelStatus.ACTIVE and lv.created_idx <= t - 1:
                    self._interact(st, lv, t, times, h, l, c, close_t, name, tf, h_cfg)
            # 3. structure breaks
            self._breaks(st, t, times, h, l, c, close_t, name, tf, h_cfg)
            # 4. expiry at the close of bar t
            for lv in st.levels:
                if lv.status is LevelStatus.ACTIVE and t - lv.created_idx >= cfg.level_expiry_bars:
                    lv.status, lv.status_reason, lv.updated_at = LevelStatus.EXPIRED, \
                        f"no interaction for {cfg.level_expiry_bars} bars", close_t(t)

        swings_h = [self._swing(SwingKind.HIGH, i, times, h, close_t, name, tf) for i in hi_idx]
        swings_l = [self._swing(SwingKind.LOW, i, times, l, close_t, name, tf) for i in lo_idx]
        active = [lv.freeze(name, tf, h_cfg) for lv in st.levels if lv.status is LevelStatus.ACTIVE]
        eqh = tuple(x for x in active if x.source is LiquiditySource.EQUAL_HIGHS)
        eql = tuple(x for x in active if x.source is LiquiditySource.EQUAL_LOWS)
        horizon = series.as_of - step * cfg.recent_sweep_bars
        sweeps = tuple(s for s in st.sweeps if s.event_time > horizon)
        bos = [e for e in st.events if e.event_type is StructureEventType.BOS]
        choch = [e for e in st.events if e.event_type is StructureEventType.CHOCH]
        codes = [f"STRUCTURE:{tf.value}:{st.trend.value}", f"WINDOW_BARS:{w}",
                 f"SWINGS:{len(hi_idx)}H/{len(lo_idx)}L", f"BREAK_METHOD:{cfg.break_confirmation.value}"]
        if a.tail_bars < len(series):
            codes.append("HISTORY_BEFORE_LAST_GAP_IGNORED")
        if st.trend is StructureDirection.UNKNOWN:
            codes.append("DIRECTION_UNKNOWN:no_structure_break_in_window")
        if st.eq_unavailable:
            codes.append(f"EQ_TOLERANCE_UNAVAILABLE:{st.eq_unavailable}_swings:{cfg.atr_feature}_not_value")
        return StructureSnapshot(
            sym, tf, series.as_of, QualityStatus.VALID, tuple(codes), st.trend, st.trend_since, times[0], w,
            tuple(swings_h[-cfg.snapshot_max_swings:]), tuple(swings_l[-cfg.snapshot_max_swings:]),
            tuple(st.events[-cfg.recent_events:]), bos[-1] if bos else None, choch[-1] if choch else None,
            eqh, eql, tuple(active), sweeps, app.__version__, STRUCTURE_VERSION, h_cfg,
            input_fingerprint([(series, n)]))   # window + earlier bars used for ATR

    # ============================================================== pieces
    def _empty(self, series: ValidatedSeries, q: QualityStatus, codes: tuple[str, ...]) -> StructureSnapshot:
        return StructureSnapshot(series.symbol, series.timeframe, series.as_of, q, codes, StructureDirection.UNKNOWN,
                                 None, None, 0, (), (), (), None, None, (), (), (), (), app.__version__,
                                 STRUCTURE_VERSION, self.cfg.config_hash,
                                 input_fingerprint([(series, len(series))]))

    def _swing(self, kind, i, times, prices, close_t, name, tf) -> Swing:
        return Swing(kind, name, tf, times[i], close_t(i + self.cfg.swing_right), prices[i], self.cfg.swing_left,
                     self.cfg.swing_right, SWING_ALGORITHM)

    def _on_swing(self, st: _State, kind: SwingKind, i: int, ci: int, times, h, l, close_t, atr, name, tf, h_cfg):
        cfg = self.cfg
        is_high = kind is SwingKind.HIGH
        price = h[i] if is_high else l[i]
        known = close_t(ci)
        side = LiquiditySide.BUY_SIDE if is_high else LiquiditySide.SELL_SIDE
        # structure reference: the most recent confirmed swing of this kind
        if is_high:
            st.last_high, st.high_broken = (i, ci), False
        else:
            st.last_low, st.low_broken = (i, ci), False
        src = LiquiditySource.SWING_HIGH if is_high else LiquiditySource.SWING_LOW
        own = _Level(stable_id(name, tf.value, src.value, times[i].isoformat(), STRUCTURE_VERSION, h_cfg), side, src,
                     price, price, price, [(i, times[i], price)], ci, known, known)
        st.levels.append(own)
        st.swing_level[(kind, i)] = own
        # equal highs / lows
        f = atr(known)
        if not f.is_valid:
            st.eq_unavailable += 1
            return
        eq_src = LiquiditySource.EQUAL_HIGHS if is_high else LiquiditySource.EQUAL_LOWS
        clusters = [lv for lv in st.levels if lv.source is eq_src and lv.status is LevelStatus.ACTIVE
                    and i - lv.members[-1][0] <= cfg.eq_max_separation_bars
                    and abs(price - lv.ref_price) <= lv.tolerance]  # type: ignore[operator]
        if clusters:
            cl = max(clusters, key=lambda x: (x.updated_at, x.level_id))
            cl.members.append((i, times[i], price))
            ps = [m[2] for m in cl.members]
            cl.zone_low, cl.zone_high = min(ps), max(ps)
            cl.price = cl.zone_high if is_high else cl.zone_low
            cl.updated_at, cl.status_reason = known, f"member added ({len(ps)} extremes)"
            own.status, own.status_reason, own.updated_at = LevelStatus.MERGED, f"merged into {cl.level_id}", known
            return
        tol = cfg.eq_tolerance_atr * f.value  # type: ignore[operator]
        prior = [(pk, pi) for (pk, pi), lv in st.swing_level.items()
                 if pk is kind and pi < i and lv.status is LevelStatus.ACTIVE and i - pi <= cfg.eq_max_separation_bars
                 and abs(price - (h[pi] if is_high else l[pi])) <= tol]
        if not prior:
            return
        _, pi = max(prior, key=lambda x: x[1])            # most recent qualifying extreme
        pp = h[pi] if is_high else l[pi]
        lo_, hi_ = min(pp, price), max(pp, price)
        cl = _Level(stable_id(name, tf.value, eq_src.value, times[pi].isoformat(), times[i].isoformat(),
                              STRUCTURE_VERSION, h_cfg), side, eq_src, hi_ if is_high else lo_, lo_, hi_,
                    [(pi, times[pi], pp), (i, times[i], price)], ci, known, known,
                    status_reason="formed", tolerance=tol, atr=f.value, ref_price=pp)
        st.levels.append(cl)
        for member in (st.swing_level[(kind, pi)], own):
            member.status, member.status_reason, member.updated_at = LevelStatus.MERGED, \
                f"merged into {cl.level_id}", known

    def _interact(self, st: _State, lv: _Level, t: int, times, h, l, c, close_t, name, tf, h_cfg) -> None:
        """Sweep: bar t trades STRICTLY beyond the level and CLOSES back at/inside it.
        Invalidation: bar t CLOSES beyond the level. A touch (== level) is neither."""
        if lv.side is LiquiditySide.BUY_SIDE:
            pierced, extreme, back = h[t] > lv.price, h[t], c[t] <= lv.price
        else:
            pierced, extreme, back = l[t] < lv.price, l[t], c[t] >= lv.price
        if not pierced:
            return
        at = close_t(t)
        if back:
            lv.status, lv.status_reason, lv.updated_at = LevelStatus.SWEPT, f"swept by bar {times[t].isoformat()}", at
            st.sweeps.append(SweepEvent(stable_id(lv.level_id, times[t].isoformat()), name, tf, lv.level_id, lv.side,
                                        lv.price, at, times[t], extreme, c[t], SWEEP_METHOD, STRUCTURE_VERSION, h_cfg))
        else:
            lv.status, lv.status_reason, lv.updated_at = LevelStatus.INVALIDATED, \
                f"closed beyond by bar {times[t].isoformat()}", at

    def _breaks(self, st: _State, t: int, times, h, l, c, close_t, name, tf, h_cfg) -> None:
        method = self.cfg.break_confirmation
        for kind in (SwingKind.HIGH, SwingKind.LOW):         # bullish first, then bearish (documented)
            ref = st.last_high if kind is SwingKind.HIGH else st.last_low
            broken = st.high_broken if kind is SwingKind.HIGH else st.low_broken
            if ref is None or broken or ref[1] > t - 1:
                continue
            pi, ci = ref
            if kind is SwingKind.HIGH:
                level = h[pi]
                probe = c[t] if method is BreakMethod.CLOSE else h[t]
                hit, direction = probe > level, StructureDirection.BULLISH
            else:
                level = l[pi]
                probe = c[t] if method is BreakMethod.CLOSE else l[t]
                hit, direction = probe < level, StructureDirection.BEARISH
            if not hit:
                continue
            prior = st.trend
            if prior is StructureDirection.UNKNOWN:
                etype, why = StructureEventType.BREAK_UNCLASSIFIED, "DIRECTION_UNKNOWN_FIRST_BREAK_IN_WINDOW"
            elif prior is direction:
                etype, why = StructureEventType.BOS, "CONTINUATION"
            else:
                etype, why = StructureEventType.CHOCH, "CHANGE_OF_CHARACTER"
            side_txt = "HIGH" if kind is SwingKind.HIGH else "LOW"
            codes = (f"{method.value.upper()}_BEYOND_SWING_{side_txt}", f"PRIOR_{prior.value.upper()}", why)
            at = close_t(t)
            st.events.append(StructureEvent(
                stable_id(name, tf.value, direction.value, times[pi].isoformat(), times[t].isoformat(),
                          method.value, STRUCTURE_VERSION, h_cfg),
                name, tf, etype, direction, prior, level, times[pi], close_t(ci), at, times[t], probe, method,
                codes, QualityStatus.VALID, STRUCTURE_VERSION, h_cfg))
            st.trend, st.trend_since = direction, at
            if kind is SwingKind.HIGH:
                st.high_broken = True
            else:
                st.low_broken = True
