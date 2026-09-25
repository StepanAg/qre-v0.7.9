"""Bybit v5 response -> schema validation -> normalisation -> domain objects.

Two failure classes:
* MalformedResponse - the response structure is wrong (not JSON, missing
  result/list, row of wrong shape, symbol mismatch). The whole response is rejected.
* RecordIssue       - one record has invalid CONTENT (bad number, broken OHLC,
  misaligned time). The record is rejected and reported; nothing is silent.
"""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.clock import epoch_ms_to_utc
from app.data.errors import BybitApiError, MalformedResponse
from app.data.ports import RecordIssue
from app.domain.enums import Category
from app.domain.errors import DomainError
from app.domain.market import Candle, FundingRate, InstrumentInfo, Symbol, Ticker, Timeframe

# Bybit kline interval codes <-> canonical Timeframe. Only place these codes exist.
TO_BYBIT_INTERVAL = {Timeframe.M1: "1", Timeframe.M5: "5", Timeframe.M15: "15",
                     Timeframe.H1: "60", Timeframe.H4: "240", Timeframe.D1: "D"}
FROM_BYBIT_INTERVAL = {v: k for k, v in TO_BYBIT_INTERVAL.items()}


# ----------------------------------------------------------------- envelope
def parse_envelope(body: bytes, endpoint: str) -> tuple[dict, int | None]:
    """Returns (result, server_time_ms). Raises BybitApiError for retCode != 0."""
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise MalformedResponse(f"{endpoint}: invalid JSON: {e}; body={body[:120]!r}") from e
    if not isinstance(doc, dict):
        raise MalformedResponse(f"{endpoint}: top-level JSON is {type(doc).__name__}, expected object")
    code = doc.get("retCode")
    if not isinstance(code, int) or isinstance(code, bool):
        raise MalformedResponse(f"{endpoint}: missing/invalid retCode")
    if code != 0:
        raise BybitApiError(code, str(doc.get("retMsg", "")), endpoint)
    result = doc.get("result")
    if not isinstance(result, dict):
        raise MalformedResponse(f"{endpoint}: missing result object")
    t = doc.get("time")
    server_ms = t if isinstance(t, int) and not isinstance(t, bool) else None
    return result, server_ms


def _list(result: dict, endpoint: str) -> list:
    lst = result.get("list")
    if not isinstance(lst, list):
        raise MalformedResponse(f"{endpoint}: result.list missing or not a list")
    return lst


def _d(raw: Any, name: str) -> Decimal:
    if not isinstance(raw, str) or raw.strip() == "":
        raise ValueError(f"{name}: expected numeric string, got {raw!r}")
    try:
        v = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"{name}: not a number: {raw!r}") from None
    if not v.is_finite():
        raise ValueError(f"{name}: not finite: {raw!r}")
    return v


def _d_opt(raw: Any, name: str) -> Decimal | None:
    return None if raw in (None, "") else _d(raw, name)


# ------------------------------------------------------------------ klines
def parse_klines(result: dict, symbol: Symbol, timeframe: Timeframe,
                 server_now: datetime) -> tuple[list[Candle], list[RecordIssue], list[int]]:
    """Returns (candles in response order, issues, raw open times of all rows with a
    parseable timestamp). Bybit row: [startTime, open, high, low, close, volume, turnover];
    volume = base coin, turnover = quote coin (linear USDT & spot)."""
    ep = "/v5/market/kline"
    if result.get("symbol") not in (None, symbol.name):
        raise MalformedResponse(f"{ep}: symbol mismatch {result.get('symbol')!r} != {symbol.name}")
    if result.get("category") not in (None, symbol.category.value):
        raise MalformedResponse(f"{ep}: category mismatch {result.get('category')!r}")
    candles: list[Candle] = []
    issues: list[RecordIssue] = []
    times: list[int] = []
    for i, row in enumerate(_list(result, ep)):
        if not isinstance(row, list) or len(row) < 7:
            raise MalformedResponse(f"{ep}: row {i} has wrong shape: {row!r}")
        try:
            open_time = epoch_ms_to_utc(row[0])
        except ValueError as e:
            issues.append(RecordIssue("invalid_record", f"row {i}: startTime: {e}", raw=repr(row)))
            continue
        times.append(int(row[0]))
        try:
            candle = Candle(
                symbol=symbol, timeframe=timeframe, open_time=open_time,
                open=_d(row[1], "open"), high=_d(row[2], "high"), low=_d(row[3], "low"),
                close=_d(row[4], "close"), volume=_d(row[5], "volume"), turnover=_d(row[6], "turnover"),
                is_closed=open_time + timeframe.delta <= server_now,
            )
        except (ValueError, DomainError) as e:
            issues.append(RecordIssue("invalid_record", f"row {i}: {e}", open_time, repr(row)))
            continue
        candles.append(candle)
    return candles, issues, times


# ------------------------------------------------------------- instruments
def parse_instruments(result: dict, category: Category) -> tuple[list[InstrumentInfo], list[RecordIssue], str]:
    ep = "/v5/market/instruments-info"
    if result.get("category") not in (None, category.value):
        raise MalformedResponse(f"{ep}: category mismatch {result.get('category')!r}")
    out: list[InstrumentInfo] = []
    issues: list[RecordIssue] = []
    for i, it in enumerate(_list(result, ep)):
        if not isinstance(it, dict) or not isinstance(it.get("symbol"), str):
            raise MalformedResponse(f"{ep}: item {i} is not an instrument object")
        try:
            pf = it.get("priceFilter") or {}
            lf = it.get("lotSizeFilter") or {}
            lev = it.get("leverageFilter") or {}
            if category is Category.LINEAR:
                qty_step = _d(lf.get("qtyStep"), "qtyStep")
                min_notional = _d_opt(lf.get("minNotionalValue"), "minNotionalValue")
                max_market = _d_opt(lf.get("maxMktOrderQty"), "maxMktOrderQty")
            else:  # spot: basePrecision is the quantity step
                qty_step = _d(lf.get("basePrecision"), "basePrecision")
                min_notional = _d_opt(lf.get("minOrderAmt"), "minOrderAmt")
                max_market = None
            launch = it.get("launchTime")
            fi = it.get("fundingInterval")
            ps = it.get("priceScale")
            out.append(InstrumentInfo(
                symbol=Symbol(it["symbol"], category),
                tick_size=_d(pf.get("tickSize"), "tickSize"),
                qty_step=qty_step,
                min_qty=_d(lf.get("minOrderQty"), "minOrderQty"),
                min_notional=min_notional,
                base_coin=it.get("baseCoin"), quote_coin=it.get("quoteCoin"),
                settle_coin=it.get("settleCoin"), status=it.get("status"),
                contract_type=it.get("contractType"),
                price_scale=int(ps) if isinstance(ps, str) and ps.isdigit() else None,
                max_qty=_d_opt(lf.get("maxOrderQty"), "maxOrderQty"),
                max_market_qty=max_market,
                min_leverage=_d_opt(lev.get("minLeverage"), "minLeverage"),
                max_leverage=_d_opt(lev.get("maxLeverage"), "maxLeverage"),
                leverage_step=_d_opt(lev.get("leverageStep"), "leverageStep"),
                funding_interval_min=fi if isinstance(fi, int) and fi > 0 else None,
                launch_time=epoch_ms_to_utc(launch) if launch not in (None, "", "0") else None,
            ))
        except (ValueError, DomainError) as e:
            issues.append(RecordIssue("invalid_record", f"{it.get('symbol')}: {e}", raw=json.dumps(it)[:300]))
    cursor = result.get("nextPageCursor") or ""
    if not isinstance(cursor, str):
        raise MalformedResponse(f"{ep}: nextPageCursor is not a string")
    return out, issues, cursor


# ---------------------------------------------------------------- tickers
def parse_tickers(result: dict, category: Category, ts: datetime) -> tuple[list[Ticker], list[RecordIssue]]:
    ep = "/v5/market/tickers"
    out: list[Ticker] = []
    issues: list[RecordIssue] = []
    for i, it in enumerate(_list(result, ep)):
        if not isinstance(it, dict) or not isinstance(it.get("symbol"), str):
            raise MalformedResponse(f"{ep}: item {i} is not a ticker object")
        try:
            nft = it.get("nextFundingTime")
            out.append(Ticker(
                symbol=Symbol(it["symbol"], category), ts=ts,
                last=_d(it.get("lastPrice"), "lastPrice"),
                bid=_d_opt(it.get("bid1Price"), "bid1Price"), ask=_d_opt(it.get("ask1Price"), "ask1Price"),
                mark=_d_opt(it.get("markPrice"), "markPrice"), index=_d_opt(it.get("indexPrice"), "indexPrice"),
                volume_24h=_d_opt(it.get("volume24h"), "volume24h"),
                turnover_24h=_d_opt(it.get("turnover24h"), "turnover24h"),
                open_interest=_d_opt(it.get("openInterest"), "openInterest"),
                funding_rate=_d_opt(it.get("fundingRate"), "fundingRate"),
                next_funding_time=epoch_ms_to_utc(nft) if nft not in (None, "", "0") else None,
            ))
        except (ValueError, DomainError) as e:
            issues.append(RecordIssue("invalid_record", f"{it.get('symbol')}: {e}", raw=json.dumps(it)[:300]))
    return out, issues


# ---------------------------------------------------------------- funding
def parse_funding(result: dict, symbol: Symbol) -> tuple[list[FundingRate], list[RecordIssue], list[int]]:
    ep = "/v5/market/funding/history"
    out: list[FundingRate] = []
    issues: list[RecordIssue] = []
    times: list[int] = []
    for i, it in enumerate(_list(result, ep)):
        if not isinstance(it, dict):
            raise MalformedResponse(f"{ep}: item {i} is not an object")
        if it.get("symbol") != symbol.name:
            raise MalformedResponse(f"{ep}: symbol mismatch {it.get('symbol')!r} != {symbol.name}")
        try:
            t = epoch_ms_to_utc(it.get("fundingRateTimestamp"))
            times.append(int(it["fundingRateTimestamp"]))
            out.append(FundingRate(symbol, t, _d(it.get("fundingRate"), "fundingRate")))
        except (ValueError, DomainError) as e:
            issues.append(RecordIssue("invalid_record", f"funding row {i}: {e}", raw=json.dumps(it)[:300]))
    return out, issues, times


def parse_server_time(result: dict, envelope_ms: int | None) -> datetime:
    nano = result.get("timeNano")
    if isinstance(nano, str) and nano.isdigit():
        return epoch_ms_to_utc(int(nano) // 1_000_000)
    sec = result.get("timeSecond")
    if isinstance(sec, str) and sec.isdigit():
        return epoch_ms_to_utc(int(sec) * 1000)
    if envelope_ms is not None:
        return epoch_ms_to_utc(envelope_ms)
    raise MalformedResponse("/v5/market/time: no usable time field")
