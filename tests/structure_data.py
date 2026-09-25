"""Hand-built OHLC paths with KNOWN expected structure (see comments in tests).
A calm warmup (h=101, l=99, c=100: no pivots, ATR = 2) precedes each path so
that min_bars and the ATR requirement are met without creating structure."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

from app.domain.market import Candle, Symbol, Timeframe
from app.research.structure.config import StructureConfig

ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = ROOT / "config" / "structure_v1.json"
ETH = Symbol("ETHUSDT")
M15 = Timeframe.M15
T0 = datetime(2026, 3, 1, tzinfo=timezone.utc)
WARMUP = 110

# (high, low, close); open = previous close
PATH_A = [  # swings H@2(105) H@10(108) H@16(112), L@6(96) L@13(101)
    (101, 99, 100), (102, 100, 101), (105, 101, 103), (104, 100, 101), (103, 99, 100),      # 0-4
    (102, 97, 98), (101, 96, 97), (100, 97, 99), (104, 98, 103), (107, 102, 106),          # 5-9  bar9: close>105
    (108, 104, 105), (106, 103, 104), (105, 102, 103), (106, 101, 104), (110, 103, 109),   # 10-14 bar14: close>108
    (111, 105, 110), (112, 107, 111), (111, 102, 103), (104, 99, 100),                     # 15-18 bar18: close<101
]
PATH_B = [  # equal highs 106 / 105.9, sweep of them, equal lows 100 / 100
    (102, 100, 101), (104, 101, 103), (106, 102, 104), (105, 101, 102), (103, 100, 101),   # 0-4  H@2=106, L@4=100
    (104, 100, 103), (105.9, 102, 104), (105, 101, 102), (103, 100, 101),                   # 5-8  H@6=105.9, L@8=100
    (106.5, 100.5, 105), (105, 102, 103),                                                   # 9: wick 106.5, close 105
]


def cfg(**changes) -> StructureConfig:
    c = StructureConfig.from_file(CONFIG_FILE)
    return c.with_(**changes) if changes else c


def candles(path, *, warmup: int = WARMUP, tf: Timeframe = M15, symbol: Symbol = ETH, start: datetime = T0,
            missing: set[int] = frozenset()) -> list[Candle]:
    """missing: indices INTO THE PATH (after warmup) that are absent (real gaps)."""
    rows = [(101, 99, 100)] * warmup + list(path)
    out, prev = [], 100.0
    for k, (hi, lo, cl) in enumerate(rows):
        o = prev
        prev = cl
        if k - warmup in missing:
            continue
        out.append(Candle(symbol, tf, start + tf.delta * k, D(str(o)), D(str(hi)), D(str(lo)), D(str(cl)),
                          D("10"), D(str(10 * (o + cl) / 2))))
    return out


def bar_open(path_index: int, *, warmup: int = WARMUP, tf: Timeframe = M15) -> datetime:
    return T0 + tf.delta * (warmup + path_index)


def bar_close(path_index: int, **kw) -> datetime:
    tf = kw.get("tf", M15)
    return bar_open(path_index, **kw) + tf.delta


def with_open_bar(cs: list[Candle], hi, lo, cl) -> list[Candle]:
    last = cs[-1]
    return cs + [Candle(last.symbol, last.timeframe, last.open_time + last.timeframe.delta, last.close, D(str(hi)),
                        D(str(lo)), D(str(cl)), D("1"), D(str(cl)), is_closed=False)]


def poisoned(c: Candle) -> Candle:
    return replace(c, open=D("1"), high=D("999999"), low=D("1"), close=D("999999"))


def mirror(path):
    """Price-inverted path (p -> 200 - p): every bullish fact becomes bearish."""
    return [(200 - lo, 200 - hi, 200 - cl) for hi, lo, cl in path]


def random_walk(n: int, seed: int, *, tf: Timeframe = M15, symbol: Symbol = ETH) -> list[Candle]:
    import math
    import random
    rng = random.Random(seed)
    out, p = [], 2000.0
    for k in range(n):
        o = p
        c = o * math.exp(rng.gauss(0, 0.004))
        hi = max(o, c) * (1 + abs(rng.gauss(0, 0.0015)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, 0.0015)))
        p = c
        out.append(Candle(symbol, tf, T0 + tf.delta * k, D(f"{o:.2f}"), D(f"{hi:.2f}"), D(f"{lo:.2f}"),
                          D(f"{c:.2f}"), D("10"), D(f"{10 * (o + c) / 2:.4f}")))
    return out
