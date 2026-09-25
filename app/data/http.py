"""The ONLY module in the project allowed to import networking libraries.

Transport performs one raw GET. Pacing, retry and backoff live in
RequestExecutor so that no business code ever calls sleep()."""
from __future__ import annotations

import random
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol

from app.data.errors import (ConnectionFailed, HttpStatusError, MarketDataError, NetworkTimeout,
                             RateLimitError, RetriesExhausted)
from app.data.metrics import IngestionMetrics


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)
    url: str = ""


class HttpTransport(Protocol):
    def get(self, url: str, params: Mapping[str, str], timeout_s: float) -> HttpResponse: ...


class UrllibTransport:
    """stdlib implementation (no third-party dependency)."""

    USER_AGENT = "qre/0.7.0 (+market-data)"

    def get(self, url: str, params: Mapping[str, str], timeout_s: float) -> HttpResponse:
        full = f"{url}?{urllib.parse.urlencode(params)}" if params else url
        req = urllib.request.Request(full, headers={"User-Agent": self.USER_AGENT,
                                                    "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as r:  # noqa: S310 (https only)
                return HttpResponse(r.status, r.read(), dict(r.headers.items()), full)
        except urllib.error.HTTPError as e:
            return HttpResponse(e.code, e.read() or b"", dict(e.headers.items()) if e.headers else {}, full)
        except (socket.timeout, TimeoutError) as e:
            raise NetworkTimeout(f"timeout after {timeout_s}s: {full}") from e
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise NetworkTimeout(f"timeout after {timeout_s}s: {full}") from e
            raise ConnectionFailed(f"connection failed: {full}: {e.reason}") from e
        except OSError as e:
            raise ConnectionFailed(f"connection failed: {full}: {e}") from e


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay_s: float = 0.5
    max_delay_s: float = 8.0
    jitter: float = 0.2          # +-20 %
    max_rate_limit_wait_s: float = 30.0

    def delay(self, attempt: int, rng: random.Random) -> float:
        """attempt is 1-based number of the attempt that just failed."""
        d = min(self.max_delay_s, self.base_delay_s * 2 ** (attempt - 1))
        return d * (1 + rng.uniform(-self.jitter, self.jitter))


class RequestPacer:
    """Minimum spacing between requests (client-side rate discipline)."""

    def __init__(self, min_interval_s: float, monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.min_interval_s = min_interval_s
        self._monotonic = monotonic
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None and self.min_interval_s > 0:
            remaining = self.min_interval_s - (self._monotonic() - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._monotonic()


class RequestExecutor:
    """pace -> call -> classify -> retry with backoff. Raises on final failure."""

    def __init__(self, transport: HttpTransport, *, timeout_s: float = 10.0,
                 policy: RetryPolicy = RetryPolicy(), pacer: RequestPacer | None = None,
                 metrics: IngestionMetrics | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic,
                 rng: random.Random | None = None) -> None:
        self.transport = transport
        self.timeout_s = timeout_s
        self.policy = policy
        self.pacer = pacer or RequestPacer(0.1, monotonic, sleep)
        self.metrics = metrics or IngestionMetrics()
        self._sleep = sleep
        self._monotonic = monotonic
        self._rng = rng or random.Random()

    def execute(self, url: str, params: Mapping[str, str],
                interpret: Callable[[HttpResponse], object]) -> object:
        """interpret() turns a response into a value or raises MarketDataError
        (e.g. BybitApiError) whose .retryable decides whether to retry."""
        last: MarketDataError | None = None
        for attempt in range(1, self.policy.max_attempts + 1):
            self.pacer.wait()
            t0 = self._monotonic()
            self.metrics.api_requests += 1
            try:
                resp = self.transport.get(url, params, self.timeout_s)
                self.metrics.request_latency_total_s += self._monotonic() - t0
                if resp.status == 429 or resp.status == 403:
                    raise self._rate_limit(resp)
                if resp.status >= 400:
                    raise HttpStatusError(resp.status, resp.url or url,
                                          resp.body[:300].decode("utf-8", "replace"))
                return interpret(resp)
            except MarketDataError as e:
                self.metrics.record_error(type(e).__name__)
                last = e
                if isinstance(e, RateLimitError) or getattr(e, "ret_code", None) in (10006, 10429):
                    self.metrics.rate_limited += 1
                if not e.retryable or attempt == self.policy.max_attempts:
                    break
                wait = self.policy.delay(attempt, self._rng)
                reset = getattr(e, "reset_after_s", None)
                if reset is not None:
                    wait = max(wait, min(reset, self.policy.max_rate_limit_wait_s))
                self.metrics.retries += 1
                self._sleep(wait)
        assert last is not None
        if last.retryable:
            raise RetriesExhausted(self.policy.max_attempts, last) from last
        raise last

    def _rate_limit(self, resp: HttpResponse) -> MarketDataError:
        if resp.status == 403:
            # Bybit returns 403 for IP rate limit ("access too frequent") and for geo blocks.
            err = HttpStatusError(403, resp.url, resp.body[:200].decode("utf-8", "replace"))
            err.retryable = True
            return err
        reset_ms = resp.headers.get("X-Bapi-Limit-Reset-Timestamp")
        reset_s = None
        if reset_ms and reset_ms.isdigit():
            reset_s = max(0.0, int(reset_ms) / 1000 - time.time())
        return RateLimitError(f"HTTP 429 rate limited: {resp.url}", reset_s)
