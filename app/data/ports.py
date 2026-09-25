"""Provider-agnostic market-data contracts.

MarketDataProvider  - where data comes FROM (Bybit today, anything tomorrow).
MarketDataStore     - read side used by research/strategy/backtest (no SQL there).
MarketDataSink      - write side used only by ingestion (backfill).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, Sequence

from app.domain.enums import Category
from app.domain.market import Candle, FundingRate, InstrumentInfo, Symbol, Ticker, Timeframe


@dataclass(frozen=True)
class RecordIssue:
    """A diagnosable data problem. kind examples: invalid_record, conflicting_duplicate,
    out_of_order, out_of_range, gap, stored_conflict."""
    kind: str
    detail: str
    at: datetime | None = None
    raw: str = ""


@dataclass(frozen=True)
class CandleBatch:
    """Result of a (possibly multi-page) candle request: sorted ascending,
    de-duplicated. May contain ONE trailing open candle (is_closed=False)."""
    symbol: Symbol
    timeframe: Timeframe
    start: datetime
    end: datetime                       # exclusive
    candles: tuple[Candle, ...]
    issues: tuple[RecordIssue, ...] = ()
    pages: int = 0
    duplicates_in_response: int = 0

    @property
    def closed(self) -> tuple[Candle, ...]:
        return tuple(c for c in self.candles if c.is_closed)


@dataclass(frozen=True)
class FundingBatch:
    symbol: Symbol
    start: datetime
    end: datetime
    rates: tuple[FundingRate, ...]
    issues: tuple[RecordIssue, ...] = ()
    pages: int = 0


class MarketDataProvider(Protocol):
    name: str

    def server_time(self) -> datetime: ...
    def instruments(self, category: Category, symbols: Sequence[str] | None = None) -> list[InstrumentInfo]: ...
    def tickers(self, category: Category, symbols: Sequence[str] | None = None) -> list[Ticker]: ...
    def candles(self, symbol: Symbol, timeframe: Timeframe, start: datetime, end: datetime) -> CandleBatch: ...
    def funding_history(self, symbol: Symbol, start: datetime, end: datetime) -> FundingBatch: ...


@dataclass(frozen=True)
class UpsertResult:
    inserted: int
    unchanged: int                          # already stored with identical values
    conflicts: tuple[RecordIssue, ...] = () # stored value differs -> original kept


@dataclass
class BackfillRunRecord:
    run_id: str
    dataset: str                  # "candles" | "funding"
    symbol: Symbol
    timeframe: Timeframe | None
    start: datetime
    end: datetime
    started_at: datetime
    finished_at: datetime | None = None
    status: str = "running"
    pages: int = 0
    fetched: int = 0
    inserted: int = 0
    unchanged: int = 0
    conflicts: int = 0
    invalid: int = 0
    open_excluded: int = 0
    gaps: int = 0
    error: str | None = None
    issues: list[RecordIssue] = field(default_factory=list)


class MarketDataStore(Protocol):
    def get_candles(self, symbol: Symbol, timeframe: Timeframe, start: datetime, end: datetime) -> list[Candle]: ...
    def last_candles(self, symbol: Symbol, timeframe: Timeframe, end: datetime, count: int) -> list[Candle]: ...
    def candle_count(self, symbol: Symbol, timeframe: Timeframe) -> int: ...
    def candle_times(self, symbol: Symbol, timeframe: Timeframe, start: datetime, end: datetime) -> list[datetime]: ...
    def instrument(self, symbol: Symbol) -> InstrumentInfo | None: ...
    def funding_rates(self, symbol: Symbol, start: datetime, end: datetime) -> list[FundingRate]: ...


class MarketDataSink(Protocol):
    def upsert_candles(self, candles: Sequence[Candle], source: str) -> UpsertResult: ...
    def upsert_instruments(self, items: Sequence[InstrumentInfo], source: str) -> int: ...
    def upsert_funding(self, items: Sequence[FundingRate], source: str) -> UpsertResult: ...
    def record_run(self, run: BackfillRunRecord) -> None: ...
