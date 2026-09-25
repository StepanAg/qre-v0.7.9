-- Phase 4: market monitor runtime state. Nothing existing is altered.
CREATE TABLE monitor_runs (
    run_id            TEXT PRIMARY KEY,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    status            TEXT NOT NULL,
    config_json       TEXT NOT NULL,
    meta_json         TEXT NOT NULL,
    last_heartbeat_at TEXT,
    health_json       TEXT,
    summary_json      TEXT,
    stop_requested    INTEGER NOT NULL DEFAULT 0
);

-- One row per processing attempt of a (symbol, timeframe, bar); polls without a
-- new bar are not recorded. Append-only.
CREATE TABLE monitor_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT NOT NULL REFERENCES monitor_runs(run_id),
    cycle_id          TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    timeframe         TEXT NOT NULL,
    as_of_ms          INTEGER NOT NULL,
    status            TEXT NOT NULL,
    regime            TEXT,
    data_quality      TEXT,
    persisted         TEXT NOT NULL CHECK (persisted IN ('saved','duplicate','failed','not_attempted')),
    duration_ms       REAL NOT NULL,
    detail            TEXT NOT NULL DEFAULT '',
    recorded_at       TEXT NOT NULL
);
CREATE INDEX idx_monitor_events_run ON monitor_events(run_id, id);
CREATE TRIGGER monitor_events_append_only_u BEFORE UPDATE ON monitor_events
BEGIN SELECT RAISE(ABORT, 'monitor events are append-only'); END;
CREATE TRIGGER monitor_events_append_only_d BEFORE DELETE ON monitor_events
BEGIN SELECT RAISE(ABORT, 'monitor events are append-only'); END;
