"""SQLite implementation of research.ports.SetupSnapshotStore."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.core.errors import StorageError
from app.domain.market import Symbol, Timeframe
from app.domain.setup import SetupSnapshot
from app.storage.writer import SerializedWriter

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _ms(t: datetime) -> int:
    return (t - _EPOCH) // timedelta(milliseconds=1)


def _dt(ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=ms)


def _canon(d: dict) -> str:
    return json.dumps(d, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass
class SetupSaveStats:
    snapshot_inserted: bool = False
    setups_new: int = 0
    events_new: int = 0
    events_known: int = 0
    conflicts: int = 0


class SQLiteSetupStore:
    def __init__(self, writer: SerializedWriter) -> None:
        self.writer = writer
        self.last = SetupSaveStats()

    def save(self, s: SetupSnapshot) -> bool:
        payload = s.canonical_json()
        stats = SetupSaveStats()
        now = datetime.now(timezone.utc).isoformat()
        cat, sym, tf, as_of, ver, ch, fp = s.key
        seen = _ms(as_of)
        with self.writer.write() as db:
            row = db.execute(
                "SELECT payload_json FROM setup_snapshots WHERE category=? AND symbol=? AND timeframe=? AND "
                "as_of_ms=? AND engine_version=? AND config_hash=? AND input_fingerprint=?",
                (cat, sym, tf, seen, ver, ch, fp)).fetchone()
            if row is not None:
                if row[0] != payload:
                    raise StorageError("non-deterministic setup snapshot: same inputs/config, different result")
            else:
                sc, rc = s.structure_context or {}, s.regime_context or {}
                db.execute("INSERT INTO setup_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (f"sus_{uuid.uuid4().hex}", cat, sym, tf, seen, s.data_quality.value, ver, ch, fp,
                            sc.get("input_fingerprint"), rc.get("input_fingerprint"), s.code_version,
                            s.evaluated_at.isoformat(), payload, now))
                stats.snapshot_inserted = True
            for st in s.setups:
                ident = _canon(st.identity())
                ex = db.execute("SELECT identity_json FROM setups WHERE setup_id=?", (st.setup_id,)).fetchone()
                if ex is None:
                    db.execute("INSERT INTO setups VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (st.setup_id, cat, sym, tf, st.setup_type.value, st.direction.value,
                                st.trigger_event_id, _ms(st.setup_time), st.key_level, st.engine_version,
                                st.config_hash, seen, ident, now))
                    stats.setups_new += 1
                elif ex[0] != ident:
                    self._conflict(db, st.setup_id, seen, "identity", f"stored={ex[0]} new={ident}", now, stats)
                    continue
                for tr in st.transitions:
                    ev = db.execute("SELECT at_ms, reason FROM setup_events WHERE setup_id=? AND status=?",
                                    (st.setup_id, tr.status.value)).fetchone()
                    if ev is None:
                        db.execute("INSERT INTO setup_events VALUES (?,?,?,?,?,?,?)",
                                   (f"se_{st.setup_id}_{tr.status.value}", st.setup_id, tr.status.value,
                                    _ms(tr.at), tr.reason, seen, now))
                        stats.events_new += 1
                    elif (ev[0], ev[1]) == (_ms(tr.at), tr.reason):
                        stats.events_known += 1
                    else:
                        self._conflict(db, st.setup_id, seen, "transition",
                                       f"{tr.status.value}: stored {_dt(ev[0]).isoformat()} {ev[1]}, "
                                       f"new {tr.at.isoformat()} {tr.reason}", now, stats)
        self.last = stats
        return stats.snapshot_inserted

    @staticmethod
    def _conflict(db, setup_id, seen, kind, detail, now, stats) -> None:
        db.execute("INSERT OR IGNORE INTO setup_conflicts(setup_id, seen_as_of_ms, kind, detail, created_at) "
                   "VALUES (?,?,?,?,?)", (setup_id, seen, kind, detail[:2000], now))
        stats.conflicts += 1

    # ------------------------------------------------------------ queries
    def latest(self, symbol: Symbol, timeframe: Timeframe) -> SetupSnapshot | None:
        with self.writer.read() as db:
            r = db.execute("SELECT payload_json, evaluated_at FROM setup_snapshots WHERE category=? AND symbol=? "
                           "AND timeframe=? ORDER BY as_of_ms DESC, created_at DESC LIMIT 1",
                           (symbol.category.value, symbol.name, timeframe.value)).fetchone()
        return SetupSnapshot.from_dict(json.loads(r[0]), datetime.fromisoformat(r[1])) if r else None

    def load(self, key: tuple) -> SetupSnapshot | None:
        cat, sym, tf, as_of, ver, ch, fp = key
        with self.writer.read() as db:
            r = db.execute("SELECT payload_json, evaluated_at FROM setup_snapshots WHERE category=? AND symbol=? "
                           "AND timeframe=? AND as_of_ms=? AND engine_version=? AND config_hash=? AND "
                           "input_fingerprint=?", (cat, sym, tf, _ms(as_of), ver, ch, fp)).fetchone()
        return SetupSnapshot.from_dict(json.loads(r[0]), datetime.fromisoformat(r[1])) if r else None

    def setups(self, symbol: Symbol, timeframe: Timeframe, limit: int = 50, status: str | None = None,
               setup_type: str | None = None) -> list[dict]:
        """Recorded setups with their latest recorded lifecycle status."""
        q = ("SELECT s.setup_id, s.setup_type, s.direction, s.setup_time_ms, s.key_level, "
             "(SELECT e.status FROM setup_events e WHERE e.setup_id=s.setup_id ORDER BY e.at_ms DESC LIMIT 1) st "
             "FROM setups s WHERE s.category=? AND s.symbol=? AND s.timeframe=?"
             + (" AND s.setup_type=?" if setup_type else "") + " ORDER BY s.setup_time_ms DESC, s.setup_id")
        args = [symbol.category.value, symbol.name, timeframe.value] + ([setup_type] if setup_type else [])
        with self.writer.read() as db:
            rows = db.execute(q, args).fetchall()
        out = [{"setup_id": r[0], "setup_type": r[1], "direction": r[2], "setup_time": _dt(r[3]).isoformat(),
                "key_level": r[4], "status": r[5]} for r in rows]
        if status:
            out = [x for x in out if x["status"] == status]
        return out[:limit]

    def events(self, setup_id: str) -> list[dict]:
        with self.writer.read() as db:
            return [{"status": r[0], "at": _dt(r[1]).isoformat(), "reason": r[2]} for r in db.execute(
                "SELECT status, at_ms, reason FROM setup_events WHERE setup_id=? ORDER BY at_ms", (setup_id,))]

    def count(self, table: str = "setup_snapshots") -> int:
        if table not in ("setup_snapshots", "setups", "setup_events", "setup_conflicts"):
            raise ValueError(table)
        with self.writer.read() as db:
            return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def status(self) -> list[dict]:
        with self.writer.read() as db:
            rows = db.execute("SELECT symbol, timeframe, COUNT(*), MAX(as_of_ms) FROM setup_snapshots "
                              "GROUP BY category, symbol, timeframe ORDER BY symbol, timeframe").fetchall()
            n = {(r[0], r[1]): r[2] for r in db.execute(
                "SELECT symbol, timeframe, COUNT(*) FROM setups GROUP BY symbol, timeframe")}
        return [{"symbol": r[0], "timeframe": r[1], "snapshots": r[2], "last_as_of": _dt(r[3]).isoformat(),
                 "setups_recorded": n.get((r[0], r[1]), 0)} for r in rows]
