"""Market-data failures. None of these is ever converted into an empty dataset."""
from __future__ import annotations

from app.core.errors import QREError


class MarketDataError(QREError):
    retryable = False


class TransportError(MarketDataError):
    retryable = True


class NetworkTimeout(TransportError):
    pass


class ConnectionFailed(TransportError):
    pass


class HttpStatusError(MarketDataError):
    def __init__(self, status: int, url: str, body: str = "") -> None:
        self.status = status
        self.retryable = status >= 500
        hint = " (403 from Bybit can be a geo/IP block, not only a rate limit)" if status == 403 else ""
        super().__init__(f"HTTP {status} for {url}{hint}: {body[:200]}")


class RateLimitError(MarketDataError):
    retryable = True

    def __init__(self, message: str, reset_after_s: float | None = None) -> None:
        self.reset_after_s = reset_after_s
        super().__init__(message)


class BybitApiError(MarketDataError):
    # 10006 too many visits, 10016 service error / server busy, 10429 system frequency protection
    RETRYABLE_CODES = {10006, 10016, 10429}

    def __init__(self, ret_code: int, ret_msg: str, endpoint: str) -> None:
        self.ret_code = ret_code
        self.ret_msg = ret_msg
        self.endpoint = endpoint
        self.retryable = ret_code in self.RETRYABLE_CODES
        super().__init__(f"Bybit retCode={ret_code} on {endpoint}: {ret_msg}")


class MalformedResponse(MarketDataError):
    """Response is not valid JSON or does not match the expected schema."""


class PaginationError(MarketDataError):
    pass


class InsufficientHistory(MarketDataError):
    pass


class RetriesExhausted(MarketDataError):
    def __init__(self, attempts: int, last: MarketDataError) -> None:
        self.attempts = attempts
        self.last = last
        super().__init__(f"gave up after {attempts} attempts: {last}")
