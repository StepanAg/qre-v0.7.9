"""SQLite implementation of research.ports.StructureSnapshotStore."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.core.errors import StorageError
from app.domain.market import Symbol, Timeframe
from app.domain.structure import StructureEvent, StructureSnapshot, SweepEvent
from app.storage.writer import SerializedWriter

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _ms(t: datetime) -> int:
    return (t - _EPOCH) // timedelta(milliseconds=1)


def _canon(d: dict) -> str:
    return json.dumps(d, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass
class SaveStats:
    snapshot_inserted: bool = False
    events_inserted: int = 0
    events_known: int = 0
    conflicts: int = 0


class SQLiteStructureStore:
    def __init__(self, writer: SerializedWriter) -> None:
        self.writer = writer
        self.last = SaveStats()

    def save(self, s: StructureSnapshot) -> bool:
        """Idempotent. Returns True if the snapshot row was new. Same key + different
        payload = non-determinism -> StorageError. Events are recorded once."""
        payload = s.to_json()
        stats = SaveStats()
        now = datetime.now(timezone.utc).isoformat()
        cat, sym, tf, as_of, ver, ch, fp = s.key
        with self.writer.write() as db:
            row = db.execute(
                "SELECT payload_json FROM structure_snapshots WHERE category=? AND symbol=? AND timeframe=? "
                "AND as_of_ms=? AND structure_version=? AND config_hash=? AND input_fingerprint=?",
                (cat, sym, tf, _ms(as_of), ver, ch, fp)).fetchone()
            if row is not None:
                if row[0] != payload:
                    raise StorageError("non-deterministic structure snapshot: same inputs/config, different result")
            else:
                db.execute("INSERT INTO structure_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (f"ss_{uuid.uuid4().hex}", cat, sym, tf, _ms(as_of), s.direction.value,
                            s.data_quality.value, ver, ch, fp, s.code_version, payload, now))
                stats.snapshot_inserted = True
            items: list[tuple[str, str, str, datetime, float, dict]] = []
            for e in s.events:
                items.append((e.event_id, e.event_type.value, e.direction.value, e.event_time, e.level_price,
                              e.to_dict()))
            for w in s.sweeps:
                items.append((w.event_id, "sweep", w.side.value, w.event_time, w.level_price, w.to_dict()))
            for eid, kind, direction, t, price, d in items:
                ex = db.execute("SELECT kind, payload_json FROM structure_events WHERE event_id=?", (eid,)).fetchone()
                p = _canon(d)
                if ex is None:
                    db.execute("INSERT INTO structure_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (eid, cat, sym, tf, kind, direction, _ms(t), price, ver, ch, _ms(as_of), p, now))
                    stats.events_inserted += 1
                elif ex[1] == p:
                    stats.events_known += 1
                else:
                    db.execute("INSERT OR IGNORE INTO structure_event_conflicts(event_id, seen_as_of_ms, "
                               "existing_kind, new_kind, new_payload_json, created_at) VALUES (?,?,?,?,?,?)",
                               (eid, _ms(as_of), ex[0], kind, p, now))
                    stats.conflicts += 1
        self.last = stats
        return stats.snapshot_inserted

    # ------------------------------------------------------------ queries
    def latest(self, symbol: Symbol, timeframe: Timeframe) -> StructureSnapshot | None:
        with self.writer.read() as db:
            r = db.execute("SELECT payload_json FROM structure_snapshots WHERE category=? AND symbol=? AND "
                           "timeframe=? ORDER BY as_of_ms DESC, created_at DESC LIMIT 1",
                           (symbol.category.value, symbol.name, timeframe.value)).fetchone()
        return StructureSnapshot.from_json(r[0]) if r else None

    def load(self, key: tuple) -> StructureSnapshot | None:
        cat, sym, tf, as_of, ver, ch, fp = key
        with self.writer.read() as db:
            r = db.execute("SELECT payload_json FROM structure_snapshots WHERE category=? AND symbol=? AND "
                           "timeframe=? AND as_of_ms=? AND structure_version=? AND config_hash=? AND "
                           "input_fingerprint=?", (cat, sym, tf, _ms(as_of), ver, ch, fp)).fetchone()
        return StructureSnapshot.from_json(r[0]) if r else None

    def events(self, symbol: Symbol, timeframe: Timeframe, limit: int = 50,
               kinds: tuple[str, ...] | None = None) -> list[StructureEvent | SweepEvent]:
        q = ("SELECT kind, payload_json FROM structure_events WHERE category=? AND symbol=? AND timeframe=?"
             + (f" AND kind IN ({','.join('?' * len(kinds))})" if kinds else "")
             + " ORDER BY event_time_ms DESC, event_id LIMIT ?")
        args = [symbol.category.value, symbol.name, timeframe.value, *(kinds or ()), limit]
        with self.writer.read() as db:
            rows = db.execute(q, args).fetchall()
        return [SweepEvent.from_dict(json.loads(p)) if k == "sweep" else StructureEvent.from_dict(json.loads(p))
                for k, p in rows]

    def count(self, table: str = "structure_snapshots") -> int:
        if table not in ("structure_snapshots", "structure_events", "structure_event_conflicts"):
            raise ValueError(table)
        with self.writer.read() as db:
            return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def status(self) -> list[dict]:
        with self.writer.read() as db:
            rows = db.execute(
                "SELECT symbol, timeframe, COUNT(*), MAX(as_of_ms) FROM structure_snapshots "
                "GROUP BY category, symbol, timeframe ORDER BY symbol, timeframe").fetchall()
            ev = {(r[0], r[1]): r[2] for r in db.execute(
                "SELECT symbol, timeframe, COUNT(*) FROM structure_events GROUP BY symbol, timeframe")}
        return [{"symbol": r[0], "timeframe": r[1], "snapshots": r[2],
                 "last_as_of": (_EPOCH + timedelta(milliseconds=r[3])).isoformat(), "events": ev.get((r[0], r[1]), 0)}
                for r in rows]
