"""SQLite persistence of research datasets and runs (research.ports.ResearchStore).
Every run is written in ONE transaction, identified deterministically and compared
by result hash: a rerun that reproduces the result is a no-op, one that does not is
a recorded conflict + StorageError (the stored result is never overwritten)."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from app.core.errors import StorageError
from app.storage.writer import SerializedWriter

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _ms(t: datetime) -> int:
    return (t - _EPOCH) // timedelta(milliseconds=1)


def _j(d) -> str:
    return json.dumps(d, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def result_hash(payload) -> str:
    return hashlib.sha256(_j(payload).encode()).hexdigest()[:32]


class SQLiteResearchStore:
    def __init__(self, writer: SerializedWriter) -> None:
        self.writer = writer

    # ------------------------------------------------------------ datasets
    def save_dataset(self, manifest: dict) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with self.writer.write() as db:
            row = db.execute("SELECT manifest_json FROM research_datasets WHERE dataset_id=?",
                             (manifest["dataset_id"],)).fetchone()
            if row is not None:
                if row[0] != _j(manifest):
                    raise StorageError(f"dataset {manifest['dataset_id']} exists with a different manifest")
                return False
            db.execute("INSERT INTO research_datasets VALUES (?,?,?,?)",
                       (manifest["dataset_id"], _j(manifest["spec"]), _j(manifest), now))
            return True

    def dataset(self, dataset_id: str) -> dict | None:
        with self.writer.read() as db:
            r = db.execute("SELECT manifest_json FROM research_datasets WHERE dataset_id=?", (dataset_id,)).fetchone()
        return json.loads(r[0]) if r else None

    def datasets(self) -> list[dict]:
        with self.writer.read() as db:
            return [json.loads(r[0]) for r in db.execute(
                "SELECT manifest_json FROM research_datasets ORDER BY created_at DESC")]

    # --------------------------------------------------------------- runs
    def save_run(self, *, run_id: str, kind: str, parent_id: str, dataset_id: str, config: dict, versions: dict,
                 summary: dict, full_result: dict, observations=(), outcomes=(), trades=(), equity=(),
                 reports=()) -> bool:
        h = result_hash(full_result)
        now = datetime.now(timezone.utc).isoformat()
        with self.writer.write() as db:
            row = db.execute("SELECT result_hash FROM research_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is not None:
                if row[0] == h:
                    return False
                db.execute("INSERT INTO research_conflicts(run_id, stored_hash, new_hash, created_at) VALUES "
                           "(?,?,?,?)", (run_id, row[0], h, now))
                conflict = True
            else:
                conflict = False
                db.execute("INSERT INTO research_runs VALUES (?,?,?,?,?,?,?,?,?)",
                           (run_id, kind, parent_id, dataset_id, _j(config), _j(versions), _j(summary), h, now))
                db.executemany("INSERT INTO replay_observations VALUES (?,?,?,?,?)",
                               [(run_id, o["setup"]["setup_id"], o["setup"]["symbol"],
                                 _ms(datetime.fromisoformat(o["setup"]["setup_time"])), _j(o)) for o in observations])
                db.executemany("INSERT INTO outcome_observations VALUES (?,?,?,?,?)",
                               [(run_id, o["setup_id"], o["horizon"], o["status"], _j(o)) for o in outcomes])
                db.executemany("INSERT INTO sim_trades VALUES (?,?,?,?,?,?,?,?,?)",
                               [(run_id, t["trade_no"], t["dataset_id"], t["symbol"], t["setup_id"],
                                 _ms(datetime.fromisoformat(t["entry_time"])),
                                 _ms(datetime.fromisoformat(t["exit_time"])), t["net_pnl"], _j(t)) for t in trades])
                db.executemany("INSERT INTO sim_equity VALUES (?,?,?)",
                               [(run_id, _ms(t), v) for t, v in equity])
                db.executemany("INSERT INTO metric_reports VALUES (?,?,?,?,?)",
                               [(f"{run_id}:{k}", run_id, k, _j(r), now) for k, r in reports])
        if conflict:
            raise StorageError(f"run {run_id}: rerun produced a different result (stored {row[0]}, new {h}); "
                               "the stored result is kept and the conflict recorded")
        return True

    def run(self, run_id: str) -> dict | None:
        with self.writer.read() as db:
            r = db.execute("SELECT * FROM research_runs WHERE run_id=?", (run_id,)).fetchone()
            if r is None:
                return None
            reps = {k: json.loads(v) for k, v in db.execute(
                "SELECT kind, report_json FROM metric_reports WHERE run_id=?", (run_id,))}
        d = dict(r)
        for k in ("config_json", "versions_json", "summary_json"):
            d[k.removesuffix("_json")] = json.loads(d.pop(k))
        d["reports"] = reps
        return d

    def runs(self, kind: str | None = None) -> list[dict]:
        q = "SELECT run_id, kind, parent_id, dataset_id, created_at FROM research_runs"
        with self.writer.read() as db:
            rows = db.execute(q + (" WHERE kind=?" if kind else "") + " ORDER BY created_at DESC",
                              (kind,) if kind else ()).fetchall()
        return [dict(r) for r in rows]

    def observations(self, run_id: str) -> list[dict]:
        with self.writer.read() as db:
            return [json.loads(r[0]) for r in db.execute(
                "SELECT payload_json FROM replay_observations WHERE run_id=? ORDER BY setup_time_ms, setup_id",
                (run_id,))]

    def outcomes(self, run_id: str) -> list[dict]:
        with self.writer.read() as db:
            return [json.loads(r[0]) for r in db.execute(
                "SELECT payload_json FROM outcome_observations WHERE run_id=? ORDER BY setup_id, horizon", (run_id,))]

    def trades(self, run_id: str) -> list[dict]:
        with self.writer.read() as db:
            return [json.loads(r[0]) for r in db.execute(
                "SELECT payload_json FROM sim_trades WHERE run_id=? ORDER BY trade_no", (run_id,))]

    def counts(self) -> dict:
        with self.writer.read() as db:
            return {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in
                    ("research_datasets", "research_runs", "replay_observations", "outcome_observations",
                     "sim_trades", "metric_reports", "research_conflicts")}
