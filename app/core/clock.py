"""Time source abstraction. All timestamps are timezone-aware UTC."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """Deterministic clock for tests and replay."""

    def __init__(self, at: datetime) -> None:
        self._at = ensure_utc(at)

    def now(self) -> datetime:
        return self._at

    def advance(self, seconds: float) -> None:
        self._at = self._at + timedelta(seconds=seconds)


def ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("naive datetime is not allowed; use timezone-aware UTC")
    return ts.astimezone(timezone.utc)


def from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def to_ms(ts: datetime) -> int:
    return int(ensure_utc(ts).timestamp() * 1000)


# ---------------------------------------------------------------- normalisation
# Internal form: timezone-aware UTC datetime (domain) / int epoch ms (storage).
_MIN_MS = 1_230_768_000_000   # 2009-01-01
_MAX_MS = 4_102_444_800_000   # 2100-01-01


def epoch_ms_to_utc(value: int | str) -> datetime:
    """Parse an epoch-MILLISECONDS value (int or digit string, as Bybit sends).

    Rejects floats, naive guessing and values outside 2009..2100, which catches
    the classic seconds-vs-milliseconds mix-up (seconds look like 1970)."""
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"epoch ms must be int or digit string, got {value!r}")
    if isinstance(value, str):
        v = value.strip()
        if not v.isdigit():
            raise ValueError(f"epoch ms must be digits, got {value!r}")
        value = int(v)
    if not isinstance(value, int):
        raise ValueError(f"epoch ms must be int, got {type(value).__name__}")
    if not _MIN_MS <= value <= _MAX_MS:
        hint = " (looks like epoch SECONDS)" if _MIN_MS // 1000 <= value <= _MAX_MS // 1000 else ""
        raise ValueError(f"epoch ms {value} out of range{hint}")
    return datetime.fromtimestamp(0, tz=timezone.utc) + timedelta(milliseconds=value)


def epoch_seconds_to_utc(value: int) -> datetime:
    """Explicit seconds conversion; there is no auto-detection by magnitude."""
    return epoch_ms_to_utc(int(value) * 1000)


def utc_to_epoch_ms(ts: datetime) -> int:
    return (ensure_utc(ts) - datetime.fromtimestamp(0, tz=timezone.utc)) // timedelta(milliseconds=1)


def normalize_timestamp(value: datetime | int | str) -> datetime:
    """Single entry point: aware datetime (any tz) or epoch-ms -> UTC datetime.
    Naive datetimes are rejected: their timezone is unknowable."""
    if isinstance(value, datetime):
        return ensure_utc(value)
    return epoch_ms_to_utc(value)
