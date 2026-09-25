"""SQLite implementation of research.ports.FeatureSnapshotStore."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.core.errors import StorageError
from app.domain.enums import Category, FeatureStatus, GapSeverity, QualityStatus
from app.domain.market import Symbol, Timeframe
from app.domain.research import Feature, FeatureSnapshot
from app.storage.writer import SerializedWriter

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _ms(t: datetime) -> int:
    return (t - _EPOCH) // timedelta(milliseconds=1)


class SQLiteFeatureSnapshotStore:
    def __init__(self, writer: SerializedWriter) -> None:
        self.writer = writer

    def save(self, s: FeatureSnapshot) -> bool:
        """Idempotent. Returns False if an identical snapshot already exists.
        Same key with DIFFERENT values means non-determinism -> StorageError."""
        if s.timeframe is None or not s.feature_set_hash or not s.input_fingerprint:
            raise StorageError("snapshot lacks timeframe / feature_set_hash / input_fingerprint")
        existing = self.load(s.symbol, s.timeframe, s.as_of, s.feature_set_hash, s.input_fingerprint)
        if existing is not None:
            if existing.features != dict(s.features):
                raise StorageError("non-deterministic feature snapshot: same inputs, different values")
            return False
        sid = f"fs_{uuid.uuid4().hex}"
        with self.writer.write() as db:
            db.execute("INSERT INTO feature_snapshots VALUES (?,?,?,?,?,?,?,?,?)",
                       (sid, s.symbol.category.value, s.symbol.name, s.timeframe.value, _ms(s.as_of),
                        s.feature_set_hash, s.input_fingerprint, s.engine_version,
                        datetime.now(timezone.utc).isoformat()))
            db.executemany("INSERT INTO feature_values VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
                (sid, key, f.name, f.timeframe.value, f.version, f.status.value, f.value, f.unit, f.lookback,
                 f.min_observations, f.bars_used, f.quality.value, f.gap_severity.value, f.reason)
                for key, f in s.features.items()])
        return True

    def load(self, symbol: Symbol, timeframe: Timeframe, as_of: datetime, feature_set_hash: str,
             input_fingerprint: str) -> FeatureSnapshot | None:
        with self.writer.read() as db:
            h = db.execute(
                "SELECT * FROM feature_snapshots WHERE category=? AND symbol=? AND timeframe=? AND as_of_ms=? "
                "AND feature_set_hash=? AND input_fingerprint=?",
                (symbol.category.value, symbol.name, timeframe.value, _ms(as_of), feature_set_hash,
                 input_fingerprint)).fetchone()
            if h is None:
                return None
            rows = db.execute("SELECT * FROM feature_values WHERE snapshot_id=? ORDER BY feature_key",
                              (h["snapshot_id"],)).fetchall()
        feats = {r["feature_key"]: Feature(
            name=r["name"], value=r["value"], timeframe=Timeframe(r["timeframe"]), as_of=as_of,
            status=FeatureStatus(r["status"]), version=r["version"], lookback=r["lookback"],
            min_observations=r["min_observations"], bars_used=r["bars_used"], unit=r["unit"],
            reason=r["reason"], quality=QualityStatus(r["quality"]),
            gap_severity=GapSeverity(r["gap_severity"])) for r in rows}
        return FeatureSnapshot(Symbol(h["symbol"], Category(h["category"])), as_of, feats, timeframe,
                               h["feature_set_hash"], h["input_fingerprint"], h["engine_version"])

    def count(self) -> int:
        with self.writer.read() as db:
            return db.execute("SELECT COUNT(*) FROM feature_snapshots").fetchone()[0]
