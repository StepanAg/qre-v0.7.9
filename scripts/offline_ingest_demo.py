#!/usr/bin/env python3
"""Offline ingestion run against the SYNTHETIC exchange (no network).
Exercises the real wiring (executor -> client -> provider -> backfill -> SQLite)
and prints technical metrics. Latency numbers here are in-process, not Bybit's."""
import json
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.cli.data_cmds import build                       # noqa: E402
from app.config.settings import load_settings             # noqa: E402
from app.core.clock import FixedClock                     # noqa: E402
from app.domain.enums import Category                     # noqa: E402
from app.domain.market import Symbol, Timeframe           # noqa: E402
from tests.fakes import T0, SyntheticBybit                # noqa: E402

now = T0 + timedelta(days=60, minutes=7)
missing = {1767225600000 + 900_000 * k for k in (3400, 3401, 4500)}  # inside the 30-day window
with tempfile.TemporaryDirectory() as d:
    s = load_settings(environ={"DB_PATH": f"{d}/demo.sqlite3"}, env_file=None)
    svc, store, m = build(s, transport=SyntheticBybit(now, T0, missing=missing))
    svc.clock = svc.provider.clock = FixedClock(now)
    svc.provider.client.executor.pacer.min_interval_s = 0
    svc.sync_instruments(Category.LINEAR, ["BTCUSDT"])
    for name in ("BTCUSDT", "ETHUSDT"):
        for tf in (Timeframe.M15, Timeframe.H1):
            r = svc.backfill(Symbol(name), tf, now - timedelta(days=30), now)
            print(f"{name} {tf.value}: {r.status} pages={r.pages} inserted={r.inserted} gaps={len(r.gaps)}")
    r2 = svc.backfill(Symbol("BTCUSDT"), Timeframe.M15, now - timedelta(days=30), now)
    print(f"repeat BTCUSDT 15m: inserted={r2.inserted} unchanged={r2.unchanged}")
    from app.data.errors import InsufficientHistory
    from app.research.service import FeatureService
    per_value = FeatureService(store).bars_per_value(["ema_200"])   # 801: owned by feature requirements
    try:
        plan, _, candles = svc.ensure_history(Symbol("BTCUSDT"), Timeframe.H1, usable_bars=100, bars_per_value=per_value)
        print(plan.describe(), "-> available", len(candles))
    except InsufficientHistory as e:   # expected here: synthetic gaps inside the warmup window
        print("ensure_history refused:", e)
    plan, _, candles = svc.ensure_history(Symbol("BTCUSDT"), Timeframe.M15, usable_bars=50, bars_per_value=per_value)
    print(plan.describe(), "-> available", len(candles))
    print(json.dumps(m.snapshot(), indent=2))
