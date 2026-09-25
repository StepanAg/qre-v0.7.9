"""Thin Bybit REST client: URL building + envelope interpretation. All network
discipline (pacing, retry, backoff, rate limits) is delegated to RequestExecutor."""
from __future__ import annotations

from typing import Mapping

from app.data.bybit.parser import parse_envelope
from app.data.http import HttpResponse, RequestExecutor

ALLOWED_ENDPOINTS = frozenset({
    "/v5/market/time",
    "/v5/market/instruments-info",
    "/v5/market/kline",
    "/v5/market/tickers",
    "/v5/market/funding/history",
})


class BybitRestClient:
    def __init__(self, base_url: str, executor: RequestExecutor) -> None:
        if not base_url.startswith("https://"):
            raise ValueError("Bybit base_url must be https")
        self.base_url = base_url.rstrip("/")
        self.executor = executor

    def get(self, endpoint: str, params: Mapping[str, str]) -> tuple[dict, int | None]:
        """Public GET only. Any endpoint outside the market-data allow-list is refused
        (defence in depth: this client can never reach order/account endpoints)."""
        if endpoint not in ALLOWED_ENDPOINTS:
            raise ValueError(f"endpoint {endpoint} is not an allowed public market-data endpoint")

        def interpret(resp: HttpResponse):
            return parse_envelope(resp.body, endpoint)

        return self.executor.execute(self.base_url + endpoint, dict(params), interpret)  # type: ignore[return-value]
