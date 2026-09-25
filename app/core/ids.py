"""Stable identifiers and idempotency keys.

Every entity gets a local id at creation. Exchange ids are stored separately and
never replace local ids (local state != exchange state).
"""
from __future__ import annotations

import hashlib
import uuid

_PREFIXES = {"sig", "set", "ent", "rsk", "ord", "oev", "pev", "trd", "fnd", "evt", "cor", "snp"}


def new_id(prefix: str) -> str:
    if prefix not in _PREFIXES:
        raise ValueError(f"unknown id prefix: {prefix!r}")
    return f"{prefix}_{uuid.uuid4().hex}"


def new_client_order_id() -> str:
    """Id sent to the exchange (Bybit orderLinkId, max 36 chars).

    Generated and persisted BEFORE submission so a restart can find the order
    on the exchange even if the ack was never received.
    """
    return f"q{uuid.uuid4().hex[:31]}"


def idempotency_key(*parts: object) -> str:
    """Deterministic key: the same logical event always yields the same key."""
    if not parts:
        raise ValueError("idempotency_key requires at least one part")
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]
