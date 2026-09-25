"""SQLite implementation of research.ports.RegimeSnapshotStore."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.core.errors import StorageError
from app.domain.market import Symbol, Timeframe
from app.domain.regime import RegimeSnapshot
from app.storage.writer import SerializedWriter

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _ms(t: datetime) -> int:
    return (t - _EPOCH) // timedelta(milliseconds=1)


class SQLiteRegimeSnapshotStore:
    def __init__(self, writer: SerializedWriter) -> None:
        self.writer = writer

    def save(self, s: RegimeSnapshot) -> bool:
        """Idempotent: True if inserted, False if the identical snapshot exists.
        Same key with a different payload = non-determinism -> StorageError."""
        payload = s.to_json()
        existing = self._load_payload(s.key)
        if existing is not None:
            if existing != payload:
                raise StorageError("non-deterministic regime snapshot: same inputs/rules/config, "
                                   "different result")
            return False
        ctx = s.btc_context
        with self.writer.write() as db:
            db.execute(
                "INSERT INTO regime_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"rs_{uuid.uuid4().hex}", s.symbol.category.value, s.symbol.name, s.timeframe.value,
                 _ms(s.as_of), s.regime.value, s.trend_direction.value, s.trend_strength.value,
                 s.volatility_state.value, s.data_quality.value, s.mtf_alignment.value,
                 ctx.status.value if ctx else None, ctx.regime.value if ctx else None,
                 s.regime_version, s.config_hash, s.feature_version, s.code_version, s.input_fingerprint,
                 payload, datetime.now(timezone.utc).isoformat()))
        return True

    def _load_payload(self, key: tuple) -> str | None:
        cat, sym, tf, as_of, rv, ch, fp = key
        with self.writer.read() as db:
            r = db.execute(
                "SELECT payload_json FROM regime_snapshots WHERE category=? AND symbol=? AND timeframe=? "
                "AND as_of_ms=? AND regime_version=? AND config_hash=? AND input_fingerprint=?",
                (cat, sym, tf, _ms(as_of), rv, ch, fp)).fetchone()
        return r[0] if r else None

    def load(self, key: tuple) -> RegimeSnapshot | None:
        p = self._load_payload(key)
        return RegimeSnapshot.from_json(p) if p is not None else None

    def history(self, symbol: Symbol, timeframe: Timeframe, limit: int = 50) -> list[RegimeSnapshot]:
        with self.writer.read() as db:
            rows = db.execute(
                "SELECT payload_json FROM regime_snapshots WHERE category=? AND symbol=? AND timeframe=? "
                "ORDER BY as_of_ms DESC, created_at DESC LIMIT ?",
                (symbol.category.value, symbol.name, timeframe.value, limit)).fetchall()
        return [RegimeSnapshot.from_json(r[0]) for r in rows]

    def count(self) -> int:
        with self.writer.read() as db:
            return db.execute("SELECT COUNT(*) FROM regime_snapshots").fetchone()[0]
