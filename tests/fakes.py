"""Offline test doubles for the Bybit adapter. Responses use the real Bybit v5
envelope/row formats (see tests/fixtures/bybit)."""
from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

from app.core.clock import FixedClock
from app.data.backfill import BackfillService
from app.data.bybit.client import BybitRestClient
from app.data.bybit.provider import BybitMarketDataProvider
from app.data.http import HttpResponse, RequestExecutor, RequestPacer, RetryPolicy
from app.data.metrics import IngestionMetrics
from app.storage.database import bootstrap
from app.storage.market_data import SQLiteMarketDataStore
from app.storage.writer import SerializedWriter

FIX = Path(__file__).resolve().parent / "fixtures" / "bybit"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
T0_MS = 1_767_225_600_000


def fixture_bytes(name: str) -> bytes:
    return (FIX / name).read_bytes()


def resp(name: str, status: int = 200, headers: Mapping[str, str] | None = None) -> HttpResponse:
    return HttpResponse(status, fixture_bytes(name), dict(headers or {}), f"fixture://{name}")


class ScriptedTransport:
    """Returns queued responses/exceptions in order; records every request."""

    def __init__(self, *items) -> None:
        self.items = list(items)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params, timeout_s):
        self.calls.append((url, dict(params)))
        if not self.items:
            raise AssertionError(f"unexpected extra request: {url} {params}")
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, s: float) -> None:
        self.calls.append(s)


class SyntheticBybit:
    """In-memory emulation of Bybit public kline/time/instruments semantics:
    newest-first, `limit` newest bars of [start, end] (both inclusive), current
    bar returned as still-forming. Deterministic prices."""

    INTERVAL_MS = {"1": 60_000, "5": 300_000, "15": 900_000, "60": 3_600_000,
                   "240": 14_400_000, "D": 86_400_000}

    def __init__(self, now: datetime, listing: datetime, *,
                 missing: set[int] | None = None, bad_value_at: set[int] | None = None,
                 price_shift: Decimal = Decimal(0)) -> None:
        self.now_ms = _ms(now)
        self.listing_raw_ms = _ms(listing)
        self.missing = missing or set()
        self.bad_value_at = bad_value_at or set()
        self.price_shift = price_shift
        self.requests: list[dict] = []

    def row(self, t: int, step: int) -> list[str]:
        i = t // step
        base = Decimal(20000) + Decimal(i % 97) * 3 + self.price_shift
        o, c = base, base + Decimal((i % 7) - 3)
        h, l = max(o, c) + 5, min(o, c) - 5
        v = Decimal("10.5") + Decimal(i % 13)
        turnover = v * (o + c) / 2
        r = [str(t), str(o), str(h), str(l), str(c), str(v), str(turnover)]
        if t in self.bad_value_at:
            r[2] = "not-a-number"
        return r

    def get(self, url, params, timeout_s):
        path = urlparse(url).path
        self.requests.append({"path": path, **params})
        if path == "/v5/market/time":
            return self._env({"timeSecond": str(self.now_ms // 1000), "timeNano": str(self.now_ms * 10**6)})
        if path == "/v5/market/instruments-info":
            doc = json.loads(fixture_bytes("instruments_linear.json"))
            doc["result"]["list"][0]["launchTime"] = str(self.listing_raw_ms)
            return HttpResponse(200, json.dumps(doc).encode(), {}, url)
        if path != "/v5/market/kline":
            return self._env({}, code=10001, msg=f"unsupported path {path}")
        step = self.INTERVAL_MS[params["interval"]]
        first_bar = -(-self.listing_raw_ms // step) * step   # first full bar after listing
        start, end, limit = int(params["start"]), int(params["end"]), int(params["limit"])
        last_bar = self.now_ms - self.now_ms % step
        t = min(end - end % step, last_bar)
        rows = []
        while t >= max(start, first_bar) and len(rows) < limit:
            if t not in self.missing:
                rows.append(self.row(t, step))
            t -= step
        return self._env({"symbol": params["symbol"], "category": params["category"], "list": rows})

    def _env(self, result, code=0, msg="OK"):
        body = {"retCode": code, "retMsg": msg, "result": result, "retExtInfo": {}, "time": self.now_ms}
        return HttpResponse(200, json.dumps(body).encode(), {}, "synthetic")

    def kline_requests(self) -> int:
        return sum(1 for r in self.requests if r["path"] == "/v5/market/kline")


def _ms(dt: datetime) -> int:
    return (dt - datetime(1970, 1, 1, tzinfo=timezone.utc)) // timedelta(milliseconds=1)


def make_executor(transport, *, attempts=4, sleep=None, metrics=None) -> RequestExecutor:
    sleep = sleep or SleepRecorder()
    return RequestExecutor(transport, timeout_s=5, policy=RetryPolicy(max_attempts=attempts),
                           pacer=RequestPacer(0, sleep=sleep), metrics=metrics or IngestionMetrics(),
                           sleep=sleep)


def make_provider(transport, *, page_limit=1000, now: datetime | None = None, **kw) -> BybitMarketDataProvider:
    ex = make_executor(transport, **kw)
    return BybitMarketDataProvider(BybitRestClient("https://api.bybit.com", ex),
                                   clock=FixedClock(now or T0 + timedelta(days=1)),
                                   kline_page_limit=page_limit, metrics=ex.metrics)


class TempDB:
    """Temporary migrated SQLite database + store; cleaned up by close()."""

    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="qre_md_"))
        self.path = self.dir / "md.sqlite3"
        self.open()

    def open(self) -> None:
        self.conn = bootstrap(self.path, shared=True)
        self.writer = SerializedWriter(self.conn)
        self.store = SQLiteMarketDataStore(self.writer)

    def reopen(self) -> None:
        self.conn.close()
        self.open()

    def close(self) -> None:
        self.conn.close()
        shutil.rmtree(self.dir, ignore_errors=True)


def make_service(transport, db: TempDB, *, now: datetime, page_limit=1000) -> BackfillService:
    p = make_provider(transport, page_limit=page_limit, now=now)
    return BackfillService(p, db.store, db.store, clock=FixedClock(now), metrics=p.metrics)
