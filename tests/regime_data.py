"""Deterministic regime scenarios (offline). Prices follow drift + seeded noise;
volatility can be scaled on the last bars. Every timeframe series is generated
independently so that each ends exactly at the common decision time."""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

from app.domain.enums import FeatureStatus, QualityStatus
from app.domain.market import Candle, Symbol, Timeframe
from app.domain.research import Feature, FeatureSnapshot
from app.research.regime.config import RegimeConfig

ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = ROOT / "config" / "regime_v1.json"
AS_OF = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)      # aligned to 15m, 1h and 4h
M15, H1, H4 = Timeframe.M15, Timeframe.H1, Timeframe.H4
BTC, ETH = Symbol("BTCUSDT"), Symbol("ETHUSDT")


def cfg() -> RegimeConfig:
    return RegimeConfig.from_file(CONFIG_FILE)


def series(kind: str, tf: Timeframe, *, n: int = 900, end: datetime = AS_OF, symbol: Symbol = ETH,
           seed: int = 1, price: float = 2000.0, sigma: float = 0.003, missing: set[int] = frozenset(),
           shock_bars: int = 20, shock_mult: float = 3.0, turn_bars: int = 40) -> list[Candle]:
    """kind: up | down | range | shock | turn_down (long up-trend then a recent decline)."""
    rng = random.Random(f"{kind}-{tf.value}-{seed}")
    start = end - tf.delta * n
    p, anchor = price, price
    out: list[Candle] = []
    for i in range(n):
        s = sigma
        if kind == "up":
            mu = 0.001
        elif kind == "down":
            mu = -0.001
        elif kind == "range":
            mu = 0.08 * math.log(anchor / p)          # mean reversion (OU in log price)
        elif kind == "shock":
            mu = 0.08 * math.log(anchor / p)
            s = sigma * (shock_mult if i >= n - shock_bars else 1.0)
        elif kind == "turn_down":
            mu = 0.001 if i < n - turn_bars else -0.0025
        else:
            raise ValueError(kind)
        o = p
        c = o * math.exp(mu + rng.gauss(0, s))
        h = max(o, c) * (1 + abs(rng.gauss(0, s / 3)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, s / 3)))
        v = 50 + rng.random() * 100
        p = c
        if i in missing:
            continue
        out.append(Candle(symbol, tf, start + tf.delta * i, D(f"{o:.2f}"), D(f"{h:.2f}"), D(f"{lo:.2f}"),
                          D(f"{c:.2f}"), D(f"{v:.4f}"), D(f"{v * (o + c) / 2:.4f}")))
    return out


# ------------------------------------------------------------ hand snapshots
def feat(name: str, value: float | None, tf: Timeframe = M15, *, status=FeatureStatus.VALUE,
         quality=QualityStatus.VALID, reason: str | None = None) -> Feature:
    if status is not FeatureStatus.VALUE:
        value, reason = None, reason or status.value
    return Feature(name, value, tf, AS_OF, status, quality=quality, reason=reason)


def snap(values: dict[str, float | Feature], tf: Timeframe = M15, symbol: Symbol = ETH,
         prefix: str = "") -> FeatureSnapshot:
    fs = {prefix + k: (v if isinstance(v, Feature) else feat(k, v, tf)) for k, v in values.items()}
    return FeatureSnapshot(symbol, AS_OF, fs, tf, "fs", "in", "1")


def atr_units(spread=0.0, slope=0.0, disp=0.0, ret=None, vol_ratio=1.0, atr_pct=0.01, **extra) -> dict:
    """Build feature values from ATR-normalised targets (atr_pct = 1 %)."""
    d = {"ema_50_200_spread": spread * atr_pct, "ema_50_slope_10": slope * atr_pct,
         "dist_ema_50_pct": disp * atr_pct, "atr_14_pct": atr_pct, "vol_ratio_20_100": vol_ratio,
         "vol_20_annualized": 0.5, "range_position_20": 0.5}
    if ret is not None:
        d["ret_20"] = ret * atr_pct
    d.update(extra)
    return d
