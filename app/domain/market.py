"""Market data entities (normalised, exchange-agnostic)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum

from app.domain.common import dec, from_epoch_ms, req, to_epoch_ms, utc
from app.domain.enums import Category
from app.domain.errors import DomainError


class Timeframe(str, Enum):
    """The ONLY timeframe representation inside the system. Exchange-specific
    codes ("15", "D") are translated in the adapter, never passed around."""
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"

    @property
    def ms(self) -> int:
        return _TF_MS[self]

    @property
    def delta(self) -> timedelta:
        return timedelta(milliseconds=self.ms)

    @classmethod
    def parse(cls, raw: "str | Timeframe") -> "Timeframe":
        if isinstance(raw, Timeframe):
            return raw
        if not isinstance(raw, str):
            raise DomainError(f"timeframe must be str, got {type(raw).__name__}")
        try:
            return cls(raw.strip().lower())
        except ValueError:
            raise DomainError(
                f"unsupported timeframe {raw!r}; use one of {[t.value for t in cls]}"
            ) from None

    def is_aligned(self, ts: datetime) -> bool:
        return to_epoch_ms(ts) % self.ms == 0

    def floor(self, ts: datetime) -> datetime:
        ms = to_epoch_ms(ts)
        return from_epoch_ms(ms - ms % self.ms)

    def ceil(self, ts: datetime) -> datetime:
        f = self.floor(ts)
        return f if f == ts else f + self.delta


_TF_MS = {Timeframe.M1: 60_000, Timeframe.M5: 300_000, Timeframe.M15: 900_000,
          Timeframe.H1: 3_600_000, Timeframe.H4: 14_400_000, Timeframe.D1: 86_400_000}


@dataclass(frozen=True)
class Symbol:
    name: str
    category: Category = Category.LINEAR

    def __post_init__(self) -> None:
        if not self.name or self.name != self.name.upper():
            raise DomainError("Symbol name must be non-empty upper-case, e.g. BTCUSDT")

    def __str__(self) -> str:
        return f"{self.category.value}:{self.name}"


@dataclass(frozen=True)
class InstrumentInfo:
    """Trading rules of an instrument, exactly as published by the exchange.
    Optional fields are None when the exchange does not publish them for this
    category (never invented)."""
    symbol: Symbol
    tick_size: Decimal                 # price step (quote currency)
    qty_step: Decimal                  # quantity step (base coin)
    min_qty: Decimal                   # base coin
    min_notional: Decimal | None       # quote currency; linear: minNotionalValue, spot: minOrderAmt
    base_coin: str | None = None
    quote_coin: str | None = None
    settle_coin: str | None = None
    status: str | None = None          # e.g. "Trading"
    contract_type: str | None = None   # linear only, e.g. "LinearPerpetual"
    price_scale: int | None = None     # decimal places of price
    max_qty: Decimal | None = None     # max limit order qty (base coin)
    max_market_qty: Decimal | None = None
    min_leverage: Decimal | None = None
    max_leverage: Decimal | None = None
    leverage_step: Decimal | None = None
    funding_interval_min: int | None = None
    launch_time: datetime | None = None

    def __post_init__(self) -> None:
        for n in ("tick_size", "qty_step", "min_qty"):
            object.__setattr__(self, n, dec(n, getattr(self, n), positive=True))
        for n in ("min_notional", "max_qty", "max_market_qty", "min_leverage", "max_leverage",
                  "leverage_step"):
            v = getattr(self, n)
            if v is not None:
                object.__setattr__(self, n, dec(n, v, non_negative=True))
        if self.launch_time is not None:
            utc("launch_time", self.launch_time)

    @property
    def is_trading(self) -> bool:
        return self.status == "Trading"

    def round_qty_down(self, qty: Decimal) -> Decimal:
        return (qty // self.qty_step) * self.qty_step

    def round_price(self, price: Decimal) -> Decimal:
        return (price / self.tick_size).to_integral_value() * self.tick_size


@dataclass(frozen=True)
class Candle:
    """OHLCV bar. Units (Bybit linear/spot):
    open/high/low/close - quote currency per 1 base coin
    volume              - BASE coin quantity traded (e.g. BTC), not USD
    turnover            - QUOTE currency notional (e.g. USDT)
    open_time is the bar START, aligned to the timeframe; the bar covers
    [open_time, open_time + timeframe)."""
    symbol: Symbol
    timeframe: Timeframe
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    turnover: Decimal = Decimal("0")
    is_closed: bool = True  # an unclosed candle must never feed closed-bar features

    def __post_init__(self) -> None:
        object.__setattr__(self, "timeframe", Timeframe.parse(self.timeframe))
        utc("open_time", self.open_time)
        if not self.timeframe.is_aligned(self.open_time):
            raise DomainError(f"{self.symbol} {self.timeframe.value}: open_time {self.open_time} "
                              "not aligned to timeframe")
        for n in ("open", "high", "low", "close"):
            object.__setattr__(self, n, dec(n, getattr(self, n), positive=True))
        for n in ("volume", "turnover"):
            object.__setattr__(self, n, dec(n, getattr(self, n), non_negative=True))
        if self.high < self.low:
            raise DomainError(f"high < low for {self.symbol} @ {self.open_time}")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise DomainError(f"inconsistent OHLC for {self.symbol} @ {self.open_time}")

    @property
    def close_time(self) -> datetime:
        """Exclusive end of the bar."""
        return self.open_time + self.timeframe.delta

    def same_values(self, other: "Candle") -> bool:
        return (self.open, self.high, self.low, self.close, self.volume, self.turnover) == \
               (other.open, other.high, other.low, other.close, other.volume, other.turnover)


@dataclass(frozen=True)
class Ticker:
    """Point-in-time market snapshot. volume_24h is BASE coin, turnover_24h is
    QUOTE currency, open_interest is BASE coin (linear only)."""
    symbol: Symbol
    ts: datetime
    last: Decimal
    bid: Decimal | None
    ask: Decimal | None
    mark: Decimal | None = None
    index: Decimal | None = None
    volume_24h: Decimal | None = None
    turnover_24h: Decimal | None = None
    open_interest: Decimal | None = None
    funding_rate: Decimal | None = None
    next_funding_time: datetime | None = None

    def __post_init__(self) -> None:
        utc("ts", self.ts)
        object.__setattr__(self, "last", dec("last", self.last, positive=True))
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise DomainError("crossed ticker: bid > ask")


@dataclass(frozen=True)
class OrderBookSnapshot:
    symbol: Symbol
    ts: datetime
    bids: tuple[tuple[Decimal, Decimal], ...]  # (price, qty), best first
    asks: tuple[tuple[Decimal, Decimal], ...]
    update_id: int | None = None

    def __post_init__(self) -> None:
        utc("ts", self.ts)
        if self.bids and self.asks and self.bids[0][0] >= self.asks[0][0]:
            raise DomainError("crossed order book")

    @property
    def mid(self) -> Decimal | None:
        if not self.bids or not self.asks:
            return None
        return (self.bids[0][0] + self.asks[0][0]) / 2


@dataclass(frozen=True)
class FundingRate:
    symbol: Symbol
    funding_time: datetime
    rate: Decimal

    def __post_init__(self) -> None:
        utc("funding_time", self.funding_time)
        object.__setattr__(self, "rate", dec("rate", self.rate))
