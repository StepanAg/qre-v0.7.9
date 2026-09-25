"""Market structure & OHLCV-derived liquidity (Phase 5).

Everything here is DERIVED FROM CLOSED CANDLES. Liquidity levels are
hypotheses about where resting orders may sit; they are not observations of
the order book. Every item carries the time it became KNOWN to the system
(confirmed_at / created_at / event_time), which is what look-ahead tests check.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from app.domain.common import req, utc
from app.domain.enums import (BreakMethod, Category, LevelStatus, LiquiditySide, LiquiditySource, QualityStatus,
                              StructureDirection, StructureEventType, SwingKind)
from app.domain.errors import DomainError
from app.domain.market import Symbol, Timeframe

_T = datetime.fromisoformat


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t else None


def _opt_t(s: str | None) -> datetime | None:
    return _T(s) if s else None


@dataclass(frozen=True)
class Swing:
    kind: SwingKind
    symbol: str
    timeframe: Timeframe
    pivot_time: datetime        # open time of the extreme bar
    confirmed_at: datetime      # close time of the bar that completed confirmation (known from here on)
    price: float
    left_bars: int
    right_bars: int
    algorithm: str              # e.g. "fractal-v1"

    def __post_init__(self) -> None:
        utc("pivot_time", self.pivot_time)
        utc("confirmed_at", self.confirmed_at)
        if self.confirmed_at <= self.pivot_time:
            raise DomainError("a swing cannot be confirmed before its pivot bar")

    def to_dict(self) -> dict:
        return {"kind": self.kind.value, "symbol": self.symbol, "timeframe": self.timeframe.value,
                "pivot_time": _iso(self.pivot_time), "confirmed_at": _iso(self.confirmed_at), "price": self.price,
                "left_bars": self.left_bars, "right_bars": self.right_bars, "algorithm": self.algorithm}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Swing":
        return cls(SwingKind(d["kind"]), d["symbol"], Timeframe(d["timeframe"]), _T(d["pivot_time"]),
                   _T(d["confirmed_at"]), d["price"], d["left_bars"], d["right_bars"], d["algorithm"])


@dataclass(frozen=True)
class StructureEvent:
    event_id: str               # stable: identity of WHAT happened (level + break bar), not its label
    symbol: str
    timeframe: Timeframe
    event_type: StructureEventType
    direction: StructureDirection          # direction of the break
    prior_direction: StructureDirection    # structure direction before the event
    level_price: float
    swing_pivot_time: datetime
    swing_confirmed_at: datetime
    event_time: datetime                   # close time of the breaking bar (known from here on)
    break_bar_time: datetime               # open time of the breaking bar
    break_price: float                     # close (CLOSE method) or high/low (WICK method)
    method: BreakMethod
    reason_codes: tuple[str, ...]
    data_quality: QualityStatus
    structure_version: str
    config_hash: str

    def __post_init__(self) -> None:
        req("event_id", self.event_id)
        if self.direction is StructureDirection.UNKNOWN:
            raise DomainError("a break always has a direction")
        if not self.swing_confirmed_at <= self.break_bar_time:
            raise DomainError("a level can only be broken by a bar that opens after the level was confirmed")
        if self.event_type is StructureEventType.BOS and self.prior_direction is not self.direction:
            raise DomainError("BOS requires prior direction == break direction")
        if self.event_type is StructureEventType.CHOCH and self.prior_direction in (self.direction,
                                                                                 StructureDirection.UNKNOWN):
            raise DomainError("CHoCH requires a known, opposite prior direction")
        if self.event_type is StructureEventType.BREAK_UNCLASSIFIED and \
                self.prior_direction is not StructureDirection.UNKNOWN:
            raise DomainError("unclassified break only when the prior direction is unknown")
        if self.data_quality is not QualityStatus.VALID:
            raise DomainError("structure events exist only on VALID data")

    def to_dict(self) -> dict:
        return {"event_id": self.event_id, "symbol": self.symbol, "timeframe": self.timeframe.value,
                "event_type": self.event_type.value, "direction": self.direction.value,
                "prior_direction": self.prior_direction.value, "level_price": self.level_price,
                "swing_pivot_time": _iso(self.swing_pivot_time), "swing_confirmed_at": _iso(self.swing_confirmed_at),
                "event_time": _iso(self.event_time), "break_bar_time": _iso(self.break_bar_time),
                "break_price": self.break_price, "method": self.method.value, "reason_codes": list(self.reason_codes),
                "data_quality": self.data_quality.value, "structure_version": self.structure_version,
                "config_hash": self.config_hash}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "StructureEvent":
        return cls(d["event_id"], d["symbol"], Timeframe(d["timeframe"]), StructureEventType(d["event_type"]),
                   StructureDirection(d["direction"]), StructureDirection(d["prior_direction"]), d["level_price"],
                   _T(d["swing_pivot_time"]), _T(d["swing_confirmed_at"]), _T(d["event_time"]),
                   _T(d["break_bar_time"]), d["break_price"], BreakMethod(d["method"]), tuple(d["reason_codes"]),
                   QualityStatus(d["data_quality"]), d["structure_version"], d["config_hash"])


@dataclass(frozen=True)
class LiquidityLevel:
    """Potential liquidity zone derived from confirmed swings (hypothesis, not order-book data)."""
    level_id: str
    symbol: str
    timeframe: Timeframe
    side: LiquiditySide
    source: LiquiditySource
    price: float                    # outer edge: max of highs (buy side) / min of lows (sell side)
    zone_low: float
    zone_high: float
    source_pivots: tuple[datetime, ...]
    source_prices: tuple[float, ...]
    created_at: datetime            # when the level became known
    updated_at: datetime            # last change (member added or status change)
    status: LevelStatus
    status_reason: str
    tolerance: float | None         # price units, equal-highs/lows only
    atr_at_creation: float | None   # the ATR the tolerance was derived from
    method: str
    data_quality: QualityStatus
    structure_version: str
    config_hash: str

    def __post_init__(self) -> None:
        if not self.zone_low <= self.price <= self.zone_high:
            raise DomainError("level price must lie in its zone")
        if len(self.source_pivots) != len(self.source_prices) or not self.source_pivots:
            raise DomainError("a level needs its source extremes")
        if self.source in (LiquiditySource.EQUAL_HIGHS, LiquiditySource.EQUAL_LOWS) and len(self.source_pivots) < 2:
            raise DomainError("equal highs/lows need at least two confirmed extremes")
        if self.updated_at < self.created_at:
            raise DomainError("updated_at before created_at")

    @property
    def is_active(self) -> bool:
        return self.status is LevelStatus.ACTIVE

    def to_dict(self) -> dict:
        return {"level_id": self.level_id, "symbol": self.symbol, "timeframe": self.timeframe.value,
                "side": self.side.value, "source": self.source.value, "price": self.price,
                "zone_low": self.zone_low, "zone_high": self.zone_high,
                "source_pivots": [_iso(t) for t in self.source_pivots], "source_prices": list(self.source_prices),
                "created_at": _iso(self.created_at), "updated_at": _iso(self.updated_at),
                "status": self.status.value, "status_reason": self.status_reason, "tolerance": self.tolerance,
                "atr_at_creation": self.atr_at_creation, "method": self.method,
                "data_quality": self.data_quality.value, "structure_version": self.structure_version,
                "config_hash": self.config_hash}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "LiquidityLevel":
        return cls(d["level_id"], d["symbol"], Timeframe(d["timeframe"]), LiquiditySide(d["side"]),
                   LiquiditySource(d["source"]), d["price"], d["zone_low"], d["zone_high"],
                   tuple(_T(t) for t in d["source_pivots"]), tuple(d["source_prices"]), _T(d["created_at"]),
                   _T(d["updated_at"]), LevelStatus(d["status"]), d["status_reason"], d["tolerance"],
                   d["atr_at_creation"], d["method"], QualityStatus(d["data_quality"]), d["structure_version"],
                   d["config_hash"])


@dataclass(frozen=True)
class SweepEvent:
    event_id: str
    symbol: str
    timeframe: Timeframe
    level_id: str
    side: LiquiditySide
    level_price: float
    event_time: datetime        # close time of the sweeping bar = confirmation
    bar_time: datetime          # open time of the sweeping bar
    extreme: float              # high (buy side) / low (sell side) that pierced the level
    close: float                # close back on the original side
    method: str                 # "wick_through_close_back_same_bar"
    structure_version: str
    config_hash: str

    def to_dict(self) -> dict:
        return {"event_id": self.event_id, "symbol": self.symbol, "timeframe": self.timeframe.value,
                "level_id": self.level_id, "side": self.side.value, "level_price": self.level_price,
                "event_time": _iso(self.event_time), "bar_time": _iso(self.bar_time), "extreme": self.extreme,
                "close": self.close, "method": self.method, "structure_version": self.structure_version,
                "config_hash": self.config_hash}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "SweepEvent":
        return cls(d["event_id"], d["symbol"], Timeframe(d["timeframe"]), d["level_id"], LiquiditySide(d["side"]),
                   d["level_price"], _T(d["event_time"]), _T(d["bar_time"]), d["extreme"], d["close"], d["method"],
                   d["structure_version"], d["config_hash"])


@dataclass(frozen=True)
class StructureSnapshot:
    symbol: Symbol
    timeframe: Timeframe
    as_of: datetime
    data_quality: QualityStatus
    reason_codes: tuple[str, ...]
    direction: StructureDirection
    direction_since: datetime | None       # event_time of the break that set the direction
    window_start: datetime | None          # first bar analysed (structure memory starts here)
    bars_used: int
    swing_highs: tuple[Swing, ...]         # most recent confirmed, oldest first
    swing_lows: tuple[Swing, ...]
    events: tuple[StructureEvent, ...]     # most recent BOS/CHoCH/unclassified breaks, oldest first
    last_bos: StructureEvent | None
    last_choch: StructureEvent | None
    equal_highs: tuple[LiquidityLevel, ...]   # ACTIVE equal-highs levels
    equal_lows: tuple[LiquidityLevel, ...]
    liquidity: tuple[LiquidityLevel, ...]     # ALL ACTIVE liquidity levels (swing + equal)
    sweeps: tuple[SweepEvent, ...]            # sweeps confirmed in the recent window
    code_version: str
    structure_version: str
    config_hash: str
    input_fingerprint: str

    def __post_init__(self) -> None:
        utc("as_of", self.as_of)
        for n in ("code_version", "structure_version", "config_hash", "input_fingerprint"):
            req(n, getattr(self, n))
        if not self.reason_codes:
            raise DomainError("a structure snapshot needs reason codes")
        if self.data_quality is not QualityStatus.VALID:
            populated = [n for n in ("swing_highs", "swing_lows", "events", "equal_highs", "equal_lows",
                                     "liquidity", "sweeps") if getattr(self, n)]
            if populated or self.direction is not StructureDirection.UNKNOWN or self.last_bos or self.last_choch:
                raise DomainError(f"no structure may be reported on {self.data_quality.value} data: {populated}")
        known = self.as_of
        for s in self.swing_highs + self.swing_lows:
            if s.confirmed_at > known:
                raise DomainError("swing confirmed after as_of (look-ahead)")
        for e in self.events:
            if e.event_time > known:
                raise DomainError("event after as_of (look-ahead)")
        for lv in self.liquidity + self.equal_highs + self.equal_lows:
            if lv.created_at > known or not lv.is_active:
                raise DomainError("snapshot levels must be ACTIVE and known at as_of")

    @property
    def key(self) -> tuple:
        return (self.symbol.category.value, self.symbol.name, self.timeframe.value, self.as_of,
                self.structure_version, self.config_hash, self.input_fingerprint)

    def to_dict(self) -> dict:
        return {"symbol": self.symbol.name, "category": self.symbol.category.value,
                "timeframe": self.timeframe.value, "as_of": _iso(self.as_of), "data_quality": self.data_quality.value,
                "reason_codes": list(self.reason_codes), "direction": self.direction.value,
                "direction_since": _iso(self.direction_since), "window_start": _iso(self.window_start),
                "bars_used": self.bars_used,
                "swing_highs": [s.to_dict() for s in self.swing_highs],
                "swing_lows": [s.to_dict() for s in self.swing_lows],
                "events": [e.to_dict() for e in self.events],
                "last_bos": self.last_bos.to_dict() if self.last_bos else None,
                "last_choch": self.last_choch.to_dict() if self.last_choch else None,
                "equal_highs": [x.to_dict() for x in self.equal_highs],
                "equal_lows": [x.to_dict() for x in self.equal_lows],
                "liquidity": [x.to_dict() for x in self.liquidity], "sweeps": [x.to_dict() for x in self.sweeps],
                "code_version": self.code_version, "structure_version": self.structure_version,
                "config_hash": self.config_hash, "input_fingerprint": self.input_fingerprint}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "StructureSnapshot":
        ev = lambda x: StructureEvent.from_dict(x) if x else None  # noqa: E731
        lv = lambda xs: tuple(LiquidityLevel.from_dict(x) for x in xs)  # noqa: E731
        return cls(Symbol(d["symbol"], Category(d["category"])), Timeframe(d["timeframe"]), _T(d["as_of"]),
                   QualityStatus(d["data_quality"]), tuple(d["reason_codes"]), StructureDirection(d["direction"]),
                   _opt_t(d["direction_since"]), _opt_t(d["window_start"]), d["bars_used"],
                   tuple(Swing.from_dict(x) for x in d["swing_highs"]),
                   tuple(Swing.from_dict(x) for x in d["swing_lows"]),
                   tuple(StructureEvent.from_dict(x) for x in d["events"]), ev(d["last_bos"]), ev(d["last_choch"]),
                   lv(d["equal_highs"]), lv(d["equal_lows"]), lv(d["liquidity"]),
                   tuple(SweepEvent.from_dict(x) for x in d["sweeps"]), d["code_version"], d["structure_version"],
                   d["config_hash"], d["input_fingerprint"])

    @classmethod
    def from_json(cls, s: str) -> "StructureSnapshot":
        return cls.from_dict(json.loads(s))
