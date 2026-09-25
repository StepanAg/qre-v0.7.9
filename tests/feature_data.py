"""Deterministic candle series for feature tests (no network, no randomness
outside a seeded generator)."""
from __future__ import annotations

import random
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D

from app.domain.market import Candle, Symbol, Timeframe

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
BTC = Symbol("BTCUSDT")


def walk(n: int, tf: Timeframe = Timeframe.M15, *, start: datetime = T0, seed: int = 7,
         missing: set[int] = frozenset(), symbol=BTC, price: float = 20000.0) -> list[Candle]:
    """Random-walk OHLCV with consistent units (turnover = volume * mid price).
    Bars whose index is in `missing` are simply absent (a real gap)."""
    rng = random.Random(seed)
    out = []
    p = price
    for i in range(n):
        o = p
        c = o * (1 + rng.gauss(0, 0.003))
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.001)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, 0.001)))
        v = 5 + rng.random() * 20
        p = c
        if i in missing:
            continue
        out.append(Candle(symbol, tf, start + tf.delta * i, D(f"{o:.2f}"), D(f"{h:.2f}"), D(f"{lo:.2f}"),
                          D(f"{c:.2f}"), D(f"{v:.4f}"), D(f"{v * (o + c) / 2:.4f}")))
    return out


def bar(i: int, o, h, l, c, v="1", q=None, tf: Timeframe = Timeframe.M15, closed=True) -> Candle:
    q = q if q is not None else str(D(v) * (D(str(o)) + D(str(c))) / 2)
    return Candle(BTC, tf, T0 + tf.delta * i, D(str(o)), D(str(h)), D(str(l)), D(str(c)), D(v), D(q),
                  is_closed=closed)


def end_of(candles: list[Candle]) -> datetime:
    return candles[-1].close_time


def poisoned(c: Candle) -> Candle:
    """Same bar with absurd values: any leak of it into a past feature is visible."""
    return replace(c, open=D("1"), high=D("999999"), low=D("1"), close=D("999999"), volume=D("1e9"),
                   turnover=D("1e14"))
