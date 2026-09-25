"""SQLite implementation of monitor.ports.MonitorRunStore (plain dicts in, no monitor imports)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.storage.writer import SerializedWriter

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _ms(t: datetime) -> int:
    return (t - _EPOCH) // timedelta(milliseconds=1)


def _j(d) -> str:
    return json.dumps(d, sort_keys=True, default=str)


class SQLiteMonitorRunStore:
    def __init__(self, writer: SerializedWriter) -> None:
        self.writer = writer

    def start_run(self, run_id: str, started_at: datetime, config: dict, meta: dict) -> None:
        with self.writer.write() as db:
            db.execute("INSERT INTO monitor_runs(run_id, started_at, status, config_json, meta_json) "
                       "VALUES (?,?,?,?,?)", (run_id, started_at.isoformat(), "running", _j(config), _j(meta)))

    def heartbeat(self, run_id: str, at: datetime, status: str, health: dict) -> None:
        with self.writer.write() as db:
            db.execute("UPDATE monitor_runs SET last_heartbeat_at=?, status=?, health_json=? WHERE run_id=?",
                       (at.isoformat(), status, _j(health), run_id))

    def record_event(self, run_id: str, e: dict) -> None:
        with self.writer.write() as db:
            db.execute(
                "INSERT INTO monitor_events(run_id, cycle_id, symbol, timeframe, as_of_ms, status, regime, "
                "data_quality, persisted, duration_ms, detail, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, e["cycle_id"], e["symbol"], e["timeframe"], _ms(e["as_of"]), e["status"], e["regime"],
                 e["data_quality"], e["persisted"], e["duration_ms"], e["detail"][:2000],
                 datetime.now(timezone.utc).isoformat()))

    def stop_requested(self, run_id: str) -> bool:
        with self.writer.read() as db:
            r = db.execute("SELECT stop_requested FROM monitor_runs WHERE run_id=?", (run_id,)).fetchone()
        return bool(r and r[0])

    def request_stop(self, run_id: str | None = None) -> list[str]:
        """Ask running monitor(s) to stop gracefully (checked every cycle)."""
        with self.writer.write() as db:
            ids = [r[0] for r in db.execute(
                "SELECT run_id FROM monitor_runs WHERE finished_at IS NULL" + (" AND run_id=?" if run_id else ""),
                (run_id,) if run_id else ())]
            db.executemany("UPDATE monitor_runs SET stop_requested=1 WHERE run_id=?", [(i,) for i in ids])
        return ids

    def mark_abandoned(self, at: datetime) -> list[str]:
        """Runs that never finished (process killed, crash, power loss) are closed as
        'abandoned' so health never shows a dead run as running. Called before a new run."""
        with self.writer.write() as db:
            ids = [r[0] for r in db.execute("SELECT run_id FROM monitor_runs WHERE finished_at IS NULL")]
            db.executemany("UPDATE monitor_runs SET finished_at=?, status='abandoned' WHERE run_id=?",
                           [(at.isoformat(), i) for i in ids])
        return ids

    def finish_run(self, run_id: str, at: datetime, status: str, health: dict, summary: dict) -> None:
        with self.writer.write() as db:
            db.execute("UPDATE monitor_runs SET finished_at=?, status=?, health_json=?, summary_json=?, "
                       "last_heartbeat_at=? WHERE run_id=?",
                       (at.isoformat(), status, _j(health), _j(summary), at.isoformat(), run_id))

    # ------------------------------------------------------------ queries
    def latest_run(self) -> dict | None:
        with self.writer.read() as db:
            r = db.execute("SELECT * FROM monitor_runs ORDER BY started_at DESC LIMIT 1").fetchone()
        return self._run(r) if r else None

    def run(self, run_id: str) -> dict | None:
        with self.writer.read() as db:
            r = db.execute("SELECT * FROM monitor_runs WHERE run_id=?", (run_id,)).fetchone()
        return self._run(r) if r else None

    @staticmethod
    def _run(r) -> dict:
        d = dict(r)
        for k in ("config_json", "meta_json", "health_json", "summary_json"):
            if d.get(k):
                d[k.removesuffix("_json")] = json.loads(d.pop(k))
            else:
                d.pop(k, None)
        return d

    def events(self, run_id: str, limit: int = 1000) -> list[dict]:
        with self.writer.read() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM monitor_events WHERE run_id=? ORDER BY id LIMIT ?", (run_id, limit))]


def read_latest_heartbeat(db_path, since: datetime) -> datetime | None:
    """Read-only, separate-connection probe used by the supervisor (another process):
    heartbeat (or start time) of the newest monitor run started at/after `since`."""
    import sqlite3
    from pathlib import Path
    if not Path(db_path).exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            r = con.execute("SELECT COALESCE(last_heartbeat_at, started_at) FROM monitor_runs "
                            "WHERE started_at >= ? ORDER BY started_at DESC LIMIT 1", (since.isoformat(),)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None                      # table not there yet / DB busy: no conclusion
    return datetime.fromisoformat(r[0]) if r and r[0] else None
