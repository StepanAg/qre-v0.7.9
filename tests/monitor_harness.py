"""Offline harness for the monitor: synthetic Bybit whose "now" follows a fake
clock, failure injection, and a waiter that advances time instead of sleeping."""
from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from app.cli.monitor_cmds import build_monitor
from app.config.settings import load_settings
from app.core.clock import FixedClock
from app.data.http import HttpResponse
from app.monitor.config import MonitorConfig
from tests.fakes import SyntheticBybit, _ms

START = datetime(2026, 3, 2, 12, 0, 30, tzinfo=timezone.utc)   # 30 s after a 15m/1h/4h close
LISTING = START - timedelta(days=400)
ROOT = Path(__file__).resolve().parents[1]


def mcfg(**kw) -> MonitorConfig:
    raw = {"schema": 1, "symbols": ["ETHUSDT"], "timeframes": ["15m", "1h"], "poll_interval_s": 30,
           "bar_close_grace_s": 10, "stale_after_s": 300, "heartbeat_interval_s": 60, "unit_max_attempts": 3,
           "unit_backoff_base_s": 5, "unit_backoff_max_s": 60, "api_down_after_failures": 2,
           "max_runtime_minutes": None}
    raw.update(kw)
    return MonitorConfig.from_mapping(raw)


class ClockedBybit(SyntheticBybit):
    """Synthetic public API: every symbol listed at `listings.get(sym, LISTING)`,
    `missing[(sym, interval)]` = absent bar open times (ms), `unknown` symbols do
    not exist, `fail_queue` injects failures (HttpResponse or Exception)."""

    def __init__(self, clock: FixedClock, *, listings=None, missing=None, unknown=(), broken=()) -> None:
        self.clock = clock
        super().__init__(clock.now(), LISTING)
        self.listings = listings or {}
        self.missing_by = missing or {}
        self.unknown = set(unknown)
        self.broken = set(broken)            # (symbol, interval) that always answer retCode 10001
        self.fail_queue: list = []
        self.kline_calls: list[tuple[str, str]] = []

    @property
    def now_ms(self):
        return _ms(self.clock.now())

    @now_ms.setter
    def now_ms(self, v):
        pass

    def fail(self, n: int, item) -> None:
        self.fail_queue += [item] * n

    def get(self, url, params, timeout_s):
        if self.fail_queue:
            item = self.fail_queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        path = urlparse(url).path
        sym = params.get("symbol", "BTCUSDT")
        if path == "/v5/market/instruments-info":
            resp = super().get(url, params, timeout_s)
            doc = json.loads(resp.body)
            if sym in self.unknown:
                doc["result"]["list"] = []
            else:
                it = doc["result"]["list"][0]
                it["symbol"], it["baseCoin"] = sym, sym.removesuffix("USDT")
                it["launchTime"] = str(_ms(self.listings.get(sym, LISTING)))
            return replace(resp, body=json.dumps(doc).encode())
        if path == "/v5/market/kline":
            self.kline_calls.append((sym, params["interval"]))
            if sym in self.unknown or (sym, params["interval"]) in self.broken:
                return self._env({}, code=10001, msg="params error: symbol invalid")
            self.listing_raw_ms = _ms(self.listings.get(sym, LISTING))
            self.missing = self.missing_by.get((sym, params["interval"]), set())
        return super().get(url, params, timeout_s)


class FakeTime:
    """Waiter: advances the fake clock instead of sleeping; optional hooks."""

    def __init__(self, clock: FixedClock) -> None:
        self.clock = clock
        self.waits = 0
        self.hooks: dict[int, callable] = {}

    def __call__(self, seconds: float) -> bool:
        self.waits += 1
        self.clock.advance(seconds)
        hook = self.hooks.get(self.waits)
        if hook:
            return bool(hook())
        return False


class Rig:
    def __init__(self, cfg: MonitorConfig | None = None, *, at: datetime = START, db_dir: Path | None = None,
                 exchange: ClockedBybit | None = None, **exchange_kw) -> None:
        self.own_dir = db_dir is None
        self.dir = db_dir or Path(tempfile.mkdtemp(prefix="qre_mon_"))
        self.clock = FixedClock(at)
        self.ex = exchange or ClockedBybit(self.clock, **exchange_kw)
        if exchange is not None:
            self.ex.clock = self.clock
        self.time = FakeTime(self.clock)
        self.settings = load_settings(environ={"DB_PATH": str(self.dir / "mon.sqlite3"), "HTTP_MAX_ATTEMPTS": "2",
                                               "HTTP_MIN_INTERVAL_MS": "0"}, env_file=None)
        self.cfg = cfg or mcfg()
        self.monitor, self.runs = build_monitor(self.settings, self.cfg, transport=self.ex, clock=self.clock,
                                                waiter=self.time)
        self.monitor.backfill.provider.client.executor._sleep = lambda s: None   # HTTP retry without real sleep
        self.market = self.monitor.market
        self.regime_store = self.monitor.regime.store

    def unit(self, key: str):
        return self.monitor.health.units[key]

    def advance(self, seconds: float):
        self.clock.advance(seconds)

    def close(self):
        self.market.writer.conn.close()
        if self.own_dir:
            shutil.rmtree(self.dir, ignore_errors=True)
