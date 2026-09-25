from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from app.domain.errors import DomainError


def dec(name: str, value: Any, *, positive: bool = False, non_negative: bool = False) -> Decimal:
    """Money/qty/price must be Decimal (or int/str). Floats are rejected to avoid
    silent binary rounding errors in PnL."""
    if isinstance(value, bool) or isinstance(value, float):
        raise DomainError(f"{name}: float/bool not allowed, use Decimal or str")
    if isinstance(value, (int, str)):
        value = Decimal(value)
    if not isinstance(value, Decimal):
        raise DomainError(f"{name}: expected Decimal, got {type(value).__name__}")
    if not value.is_finite():
        raise DomainError(f"{name}: must be finite")
    if positive and value <= 0:
        raise DomainError(f"{name}: must be > 0, got {value}")
    if non_negative and value < 0:
        raise DomainError(f"{name}: must be >= 0, got {value}")
    return value


def utc(name: str, ts: datetime) -> datetime:
    if not isinstance(ts, datetime) or ts.tzinfo is None:
        raise DomainError(f"{name}: timezone-aware datetime required")
    return ts


def req(name: str, value: str | None) -> str:
    if not value:
        raise DomainError(f"{name}: required")
    return value


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MS = timedelta(milliseconds=1)


def to_epoch_ms(ts: datetime) -> int:
    """Exact integer ms (no float rounding)."""
    utc("ts", ts)
    return (ts - EPOCH) // _MS


def from_epoch_ms(ms: int) -> datetime:
    if isinstance(ms, bool) or not isinstance(ms, int):
        raise DomainError(f"epoch ms must be int, got {type(ms).__name__}")
    return EPOCH + timedelta(milliseconds=ms)
