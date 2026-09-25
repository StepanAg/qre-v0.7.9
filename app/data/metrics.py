"""Technical ingestion metrics (NOT trading performance)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class IngestionMetrics:
    api_requests: int = 0
    api_errors: int = 0
    retries: int = 0
    rate_limited: int = 0
    request_latency_total_s: float = 0.0
    symbols_loaded: int = 0
    candles_fetched: int = 0
    candles_inserted: int = 0
    duplicates_rejected: int = 0       # same key already stored / repeated in response, same values
    conflicts: int = 0                 # same key, DIFFERENT values -> kept original, reported
    invalid_rejected: int = 0
    open_candles_excluded: int = 0
    gaps_detected: int = 0
    funding_inserted: int = 0
    backfill_duration_s: float = 0.0
    db_write_duration_s: float = 0.0
    error_kinds: dict[str, int] = field(default_factory=dict)

    def record_error(self, kind: str) -> None:
        self.api_errors += 1
        self.error_kinds[kind] = self.error_kinds.get(kind, 0) + 1

    @property
    def avg_latency_ms(self) -> float | None:
        if not self.api_requests:
            return None
        return round(self.request_latency_total_s / self.api_requests * 1000, 2)

    def snapshot(self) -> dict:
        d = asdict(self)
        d["avg_request_latency_ms"] = self.avg_latency_ms
        d["request_latency_total_s"] = round(self.request_latency_total_s, 4)
        d["backfill_duration_s"] = round(self.backfill_duration_s, 4)
        d["db_write_duration_s"] = round(self.db_write_duration_s, 4)
        return d
