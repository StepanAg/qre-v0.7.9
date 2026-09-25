"""BybitMarketDataProvider: implements the MarketDataProvider port on top of the
public REST client. Handles pagination; never returns a partial dataset silently."""
from __future__ import annotations

import math
from datetime import datetime
from typing import Sequence

from app.core.clock import Clock, SystemClock, epoch_ms_to_utc, utc_to_epoch_ms
from app.data.bybit.client import BybitRestClient
from app.data.bybit.parser import (TO_BYBIT_INTERVAL, parse_funding, parse_instruments, parse_klines,
                                   parse_server_time, parse_tickers)
from app.data.errors import MalformedResponse, PaginationError
from app.data.metrics import IngestionMetrics
from app.data.ports import CandleBatch, FundingBatch, RecordIssue
from app.data.quality import dedupe, order_issue, unit_issue
from app.domain.enums import Category
from app.domain.market import Candle, FundingRate, InstrumentInfo, Symbol, Ticker, Timeframe

FUNDING_PAGE_LIMIT = 200      # Bybit max for funding history
INSTRUMENTS_PAGE_LIMIT = 1000


class BybitMarketDataProvider:
    name = "bybit"

    def __init__(self, client: BybitRestClient, *, clock: Clock | None = None,
                 kline_page_limit: int = 1000, metrics: IngestionMetrics | None = None) -> None:
        if not 1 <= kline_page_limit <= 1000:
            raise ValueError("kline_page_limit must be in [1, 1000]")
        self.client = client
        self.clock = clock or SystemClock()
        self.page_limit = kline_page_limit
        self.metrics = metrics or client.executor.metrics

    # --------------------------------------------------------------- time
    def server_time(self) -> datetime:
        result, env_ms = self.client.get("/v5/market/time", {})
        return parse_server_time(result, env_ms)

    # -------------------------------------------------------- instruments
    def instruments(self, category: Category, symbols: Sequence[str] | None = None) -> list[InstrumentInfo]:
        out: list[InstrumentInfo] = []
        issues: list[RecordIssue] = []
        if symbols:
            for s in symbols:
                result, _ = self.client.get("/v5/market/instruments-info",
                                            {"category": category.value, "symbol": s})
                items, iss, _ = parse_instruments(result, category)
                out += items
                issues += iss
        else:
            cursor, seen = "", set()
            for _ in range(50):
                params = {"category": category.value, "limit": str(INSTRUMENTS_PAGE_LIMIT)}
                if cursor:
                    params["cursor"] = cursor
                result, _ = self.client.get("/v5/market/instruments-info", params)
                items, iss, cursor = parse_instruments(result, category)
                out += items
                issues += iss
                if not cursor:
                    break
                if cursor in seen:
                    raise PaginationError("instruments-info: cursor repeated")
                seen.add(cursor)
            else:
                raise PaginationError("instruments-info: too many pages")
        self.metrics.invalid_rejected += len(issues)
        self.last_issues = issues
        return out

    # ------------------------------------------------------------ tickers
    def tickers(self, category: Category, symbols: Sequence[str] | None = None) -> list[Ticker]:
        targets = list(symbols) if symbols else [None]
        out: list[Ticker] = []
        for s in targets:
            params = {"category": category.value}
            if s:
                params["symbol"] = s
            result, env_ms = self.client.get("/v5/market/tickers", params)
            ts = epoch_ms_to_utc(env_ms) if env_ms else self.clock.now()
            items, issues = parse_tickers(result, category, ts)
            self.metrics.invalid_rejected += len(issues)
            out += items
        return out

    # ------------------------------------------------------------ candles
    def candles(self, symbol: Symbol, timeframe: Timeframe, start: datetime, end: datetime) -> CandleBatch:
        """Fetch [start, end) walking backwards page by page (Bybit returns the
        newest `limit` bars of the window, newest first)."""
        start = timeframe.ceil(start)
        end = timeframe.ceil(end)
        if end <= start:
            return CandleBatch(symbol, timeframe, start, end, ())
        start_ms, end_ms = utc_to_epoch_ms(start), utc_to_epoch_ms(end)
        expected_bars = (end_ms - start_ms) // timeframe.ms
        max_pages = math.ceil(expected_bars / self.page_limit) + 2
        cursor_end = end_ms - 1             # Bybit `end` is inclusive
        raw: list[Candle] = []
        issues: list[RecordIssue] = []
        pages = 0
        while True:
            pages += 1
            if pages > max_pages:
                raise PaginationError(f"kline {symbol} {timeframe.value}: exceeded {max_pages} pages")
            result, env_ms = self.client.get("/v5/market/kline", {
                "category": symbol.category.value, "symbol": symbol.name,
                "interval": TO_BYBIT_INTERVAL[timeframe],
                "start": str(start_ms), "end": str(cursor_end), "limit": str(self.page_limit)})
            server_now = epoch_ms_to_utc(env_ms) if env_ms else self.clock.now()
            page, page_issues, times = parse_klines(result, symbol, timeframe, server_now)
            issues += page_issues
            oi = order_issue(times, f"kline page {pages}")
            if oi:
                issues.append(oi)
            for c in page:
                if not start <= c.open_time < end:
                    issues.append(RecordIssue("out_of_range", f"{c.open_time} outside requested range",
                                              c.open_time))
                else:
                    raw.append(c)
            if not times:
                break                        # no data (before listing or empty range)
            oldest = min(times)
            if oldest <= start_ms:
                break                        # reached the beginning of the window
            if len(times) < self.page_limit:
                break                        # exchange has nothing older in this window
            new_end = oldest - 1
            if new_end >= cursor_end:
                raise PaginationError(f"kline {symbol}: no progress at end={cursor_end}")
            cursor_end = new_end
        self.metrics.candles_fetched += len(raw)
        unique, dups, dup_issues = dedupe(raw)
        issues += dup_issues
        issues += [u for u in (unit_issue(c) for c in unique) if u is not None]
        self.metrics.invalid_rejected += sum(1 for i in issues if i.kind == "invalid_record")
        if len([c for c in unique if not c.is_closed]) > 1:
            raise MalformedResponse(f"kline {symbol}: more than one open candle in response")
        return CandleBatch(symbol, timeframe, start, end, tuple(unique), tuple(issues), pages, dups)

    # ------------------------------------------------------------ funding
    def funding_history(self, symbol: Symbol, start: datetime, end: datetime) -> FundingBatch:
        if symbol.category is not Category.LINEAR:
            raise ValueError("funding history exists only for linear contracts")
        start_ms, end_ms = utc_to_epoch_ms(start), utc_to_epoch_ms(end)
        cursor_end = end_ms - 1
        rates: dict[int, FundingRate] = {}
        issues: list[RecordIssue] = []
        pages = 0
        max_pages = math.ceil((end_ms - start_ms) / (3_600_000 * FUNDING_PAGE_LIMIT)) + 2  # >=1h intervals
        while True:
            pages += 1
            if pages > max_pages:
                raise PaginationError(f"funding {symbol}: exceeded {max_pages} pages")
            result, _ = self.client.get("/v5/market/funding/history", {
                "category": "linear", "symbol": symbol.name,
                "endTime": str(cursor_end), "limit": str(FUNDING_PAGE_LIMIT)})
            page, iss, times = parse_funding(result, symbol)
            issues += iss
            for r in page:
                ms = utc_to_epoch_ms(r.funding_time)
                if start_ms <= ms < end_ms:
                    prev = rates.get(ms)
                    if prev is not None and prev.rate != r.rate:
                        issues.append(RecordIssue("conflicting_duplicate", f"funding {r.funding_time}",
                                                  r.funding_time))
                    rates[ms] = r
            if not times or min(times) <= start_ms or len(times) < FUNDING_PAGE_LIMIT:
                break
            new_end = min(times) - 1
            if new_end >= cursor_end:
                raise PaginationError(f"funding {symbol}: no progress")
            cursor_end = new_end
        self.metrics.invalid_rejected += sum(1 for i in issues if i.kind == "invalid_record")
        return FundingBatch(symbol, start, end, tuple(rates[k] for k in sorted(rates)), tuple(issues), pages)
