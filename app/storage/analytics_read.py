"""Read-only access for Phase 8 analytics.

Opens its OWN connection in SQLite read-only mode (`mode=ro`), so a write is
impossible at the driver level, and issues SELECT statements only (AST test).
Returns domain objects where they exist (SimulatedTrade, Setup, OutcomeObservation,
Candle) and plain decoded rows otherwise."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from app.domain.enums import Category, OutcomeStatus, SimExitReason
from app.domain.market import Candle, Symbol, Timeframe
from app.domain.research_lab import OutcomeObservation, SimulatedTrade
from app.domain.setup import Setup

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _dt(ms: int | None) -> datetime | None:
    return None if ms is None else _EPOCH + timedelta(milliseconds=ms)


def _ms(t: datetime) -> int:
    return (t - _EPOCH) // timedelta(milliseconds=1)


def trade_from_payload(d: dict) -> SimulatedTrade:
    T = datetime.fromisoformat
    return SimulatedTrade(
        d["run_id"], d["trade_no"], d["dataset_id"], d["symbol"], d["setup_id"], d["setup_type"], d["direction"],
        T(d["decision_time"]), T(d["entry_time"]), d["entry_price"], d["qty"], d["stop"], d["take_profit"],
        T(d["exit_time"]), d["exit_price"], SimExitReason(d["exit_reason"]), d["bars_held"], d["gross_pnl"],
        d["fees"], d["funding"], d["slippage_cost"], d["net_pnl"], d["initial_risk"], d["r_multiple"],
        d["funding_source"], d["ambiguous"])


def outcome_from_payload(d: dict) -> OutcomeObservation:
    return OutcomeObservation(d["setup_id"], d["symbol"], d["setup_type"], d["direction"], d["anchor"],
                              datetime.fromisoformat(d["anchor_time"]), d["horizon"], OutcomeStatus(d["status"]),
                              d["reason"], d["values"])


class SQLiteAnalyticsReader:
    """Implements app.analytics.ports.AnalyticsSource structurally (storage never imports analytics)."""

    def __init__(self, db_path: Path | str) -> None:
        if not Path(db_path).exists():
            raise FileNotFoundError(f"database {db_path} does not exist")
        self.conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self.conn.close()

    def _rows(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    # ------------------------------------------------------------- generic
    def table_names(self) -> set[str]:
        return {r[0] for r in self._rows("SELECT name FROM sqlite_master WHERE type = 'table'")}

    def table_count(self, table: str) -> int:
        if table not in self.table_names():
            raise ValueError(f"unknown table {table}")
        return self._rows(f'SELECT COUNT(*) FROM "{table}"')[0][0]

    def triggers_of(self, table: str) -> list[str]:
        return [r[0] for r in self._rows("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
                                         (table,))]

    def migrations(self) -> list[str]:
        return [r[0] for r in self._rows("SELECT version FROM schema_migrations ORDER BY version")]

    # ------------------------------------------------------------ research
    def list_runs(self, kind: str | None = None) -> list[dict]:
        sql = ("SELECT run_id, kind, parent_id, dataset_id, config_json, versions_json, summary_json, result_hash, "
               "created_at FROM research_runs")
        rows = self._rows(sql + " WHERE kind = ? ORDER BY created_at, run_id", (kind,)) if kind \
            else self._rows(sql + " ORDER BY created_at, run_id")
        return [self._run(r) for r in rows]

    def run(self, run_id: str) -> dict | None:
        rows = self._rows("SELECT run_id, kind, parent_id, dataset_id, config_json, versions_json, summary_json, "
                          "result_hash, created_at FROM research_runs WHERE run_id = ?", (run_id,))
        return self._run(rows[0]) if rows else None

    @staticmethod
    def _run(r) -> dict:
        return {"run_id": r["run_id"], "kind": r["kind"], "parent_id": r["parent_id"], "dataset_id": r["dataset_id"],
                "config": json.loads(r["config_json"]), "versions": json.loads(r["versions_json"]),
                "summary": json.loads(r["summary_json"]), "result_hash": r["result_hash"],
                "created_at": r["created_at"]}

    def dataset(self, dataset_id: str) -> dict | None:
        rows = self._rows("SELECT manifest_json FROM research_datasets WHERE dataset_id = ?", (dataset_id,))
        return json.loads(rows[0][0]) if rows else None

    def trades(self, run_id: str) -> list[SimulatedTrade]:
        return [trade_from_payload(json.loads(r[0])) for r in self._rows(
            "SELECT payload_json FROM sim_trades WHERE run_id = ? ORDER BY trade_no", (run_id,))]

    def trade_rows(self, run_id: str | None = None) -> list[dict]:
        """Raw rows (for integrity checks that must see records the domain model would reject)."""
        sql = "SELECT run_id, trade_no, dataset_id, symbol, setup_id, entry_time_ms, exit_time_ms, net_pnl, payload_json " \
              "FROM sim_trades"
        rows = self._rows(sql + " WHERE run_id = ? ORDER BY trade_no", (run_id,)) if run_id \
            else self._rows(sql + " ORDER BY run_id, trade_no")
        return [{**dict(r), "payload": json.loads(r["payload_json"])} for r in rows]

    def equity(self, run_id: str) -> list[tuple[datetime, float]]:
        return [(_dt(r[0]), r[1]) for r in self._rows(
            "SELECT ts_ms, equity FROM sim_equity WHERE run_id = ? ORDER BY ts_ms", (run_id,))]

    def observations(self, run_id: str) -> list[dict]:
        out = []
        for r in self._rows("SELECT payload_json FROM replay_observations WHERE run_id = ? "
                            "ORDER BY setup_time_ms, setup_id", (run_id,)):
            d = json.loads(r[0])
            out.append({"setup": Setup.from_dict(d["setup"]), "first_seen": datetime.fromisoformat(d["first_seen"]),
                        "last_seen": datetime.fromisoformat(d["last_seen"]),
                        "first_regime_fit": d["first_regime_fit"]})
        return out

    def outcomes(self, run_id: str) -> list[OutcomeObservation]:
        return [outcome_from_payload(json.loads(r[0])) for r in self._rows(
            "SELECT payload_json FROM outcome_observations WHERE run_id = ? ORDER BY setup_id, horizon", (run_id,))]

    def outcome_rows(self) -> list[dict]:
        return [{"run_id": r[0], "setup_id": r[1], "horizon": r[2], "status": r[3], "payload": json.loads(r[4])}
                for r in self._rows("SELECT run_id, setup_id, horizon, status, payload_json FROM outcome_observations "
                                    "ORDER BY run_id, setup_id, horizon")]

    def run_ids_of(self, table: str) -> set[str]:
        if table not in ("sim_trades", "sim_equity", "replay_observations", "outcome_observations",
                         "metric_reports"):
            raise ValueError(table)
        return {r[0] for r in self._rows(f"SELECT DISTINCT run_id FROM {table}")}

    # ---------------------------------------------------------------- live
    def live_setups(self, symbol: str | None = None, timeframe: str | None = None) -> list[dict]:
        sql = ("SELECT setup_id, symbol, timeframe, setup_type, direction, setup_time_ms, key_level, engine_version, "
               "config_hash FROM setups")
        cond, args = [], []
        if symbol:
            cond.append("symbol = ?")
            args.append(symbol)
        if timeframe:
            cond.append("timeframe = ?")
            args.append(timeframe)
        rows = self._rows(sql + (" WHERE " + " AND ".join(cond) if cond else "") + " ORDER BY setup_time_ms, setup_id",
                          tuple(args))
        return [{**dict(r), "setup_time": _dt(r["setup_time_ms"])} for r in rows]

    def setup_events(self) -> list[dict]:
        return [{**dict(r), "at": _dt(r["at_ms"])} for r in self._rows(
            "SELECT setup_id, status, at_ms, reason FROM setup_events ORDER BY setup_id, at_ms")]

    def regime_snapshots(self, symbol: str | None = None, timeframe: str | None = None) -> list[dict]:
        sql = ("SELECT symbol, timeframe, as_of_ms, regime, trend_direction, volatility_state, data_quality, "
               "btc_status, btc_regime, regime_version, config_hash FROM regime_snapshots")
        cond, args = [], []
        if symbol:
            cond.append("symbol = ?")
            args.append(symbol)
        if timeframe:
            cond.append("timeframe = ?")
            args.append(timeframe)
        rows = self._rows(sql + (" WHERE " + " AND ".join(cond) if cond else "") +
                          " ORDER BY symbol, timeframe, as_of_ms", tuple(args))
        return [{**dict(r), "as_of": _dt(r["as_of_ms"])} for r in rows]

    def structure_events(self) -> list[dict]:
        return [{**dict(r), "event_time": _dt(r["event_time_ms"])} for r in self._rows(
            "SELECT event_id, symbol, timeframe, kind, direction, event_time_ms, structure_version, config_hash "
            "FROM structure_events ORDER BY symbol, timeframe, event_time_ms")]

    def snapshot_quality(self, table: str) -> dict[str, int]:
        if table not in ("regime_snapshots", "structure_snapshots", "setup_snapshots"):
            raise ValueError(table)
        return {r[0]: r[1] for r in self._rows(f"SELECT data_quality, COUNT(*) FROM {table} GROUP BY data_quality")}

    def monitor_runs(self) -> list[dict]:
        return [dict(r) for r in self._rows(
            "SELECT run_id, started_at, finished_at, status, last_heartbeat_at, config_json, health_json, summary_json "
            "FROM monitor_runs ORDER BY started_at")]

    def monitor_events(self, run_id: str | None = None) -> list[dict]:
        sql = ("SELECT run_id, symbol, timeframe, as_of_ms, status, regime, data_quality, persisted, duration_ms "
               "FROM monitor_events")
        rows = self._rows(sql + " WHERE run_id = ? ORDER BY id", (run_id,)) if run_id else self._rows(sql + " ORDER BY id")
        return [dict(r) for r in rows]

    def conflicts(self) -> dict[str, list[dict]]:
        return {"setup_conflicts": [dict(r) for r in self._rows(
                    "SELECT id, setup_id, seen_as_of_ms, kind, detail FROM setup_conflicts ORDER BY id")],
                "structure_event_conflicts": [dict(r) for r in self._rows(
                    "SELECT id, event_id, seen_as_of_ms, existing_kind, new_kind FROM structure_event_conflicts "
                    "ORDER BY id")],
                "research_conflicts": [dict(r) for r in self._rows(
                    "SELECT id, run_id, stored_hash, new_hash FROM research_conflicts ORDER BY id")]}

    # -------------------------------------------------------------- market
    def candles(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[Candle]:
        """Closed candles with start <= open_time < end (same semantics as the market data store)."""
        tf = Timeframe(timeframe)
        rows = self._rows("SELECT category, open_time_ms, open, high, low, close, volume, turnover FROM candles "
                          "WHERE symbol = ? AND timeframe = ? AND open_time_ms >= ? AND open_time_ms < ? "
                          "ORDER BY open_time_ms", (symbol, tf.value, _ms(start), _ms(end)))
        return [Candle(Symbol(symbol, Category(r[0])), tf, _dt(r[1]), Decimal(r[2]), Decimal(r[3]), Decimal(r[4]),
                       Decimal(r[5]), Decimal(r[6]), Decimal(r[7])) for r in rows]
