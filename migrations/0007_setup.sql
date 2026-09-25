-- Phase 6: setup detection. Nothing existing is altered.

-- Deterministic result per (series, as_of, engine version, config, inputs). evaluated_at is
-- metadata (wall clock) and is NOT part of the compared payload.
CREATE TABLE setup_snapshots (
    snapshot_id       TEXT PRIMARY KEY,
    category          TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    timeframe         TEXT NOT NULL CHECK (timeframe IN ('1m','5m','15m','1h','4h','1d')),
    as_of_ms          INTEGER NOT NULL,
    data_quality      TEXT NOT NULL,
    engine_version    TEXT NOT NULL,
    config_hash       TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    structure_input_fingerprint TEXT,        -- link to the StructureSnapshot the result used at as_of
    regime_input_fingerprint    TEXT,        -- link to the RegimeSnapshot context (NULL if none)
    code_version      TEXT NOT NULL,
    evaluated_at      TEXT NOT NULL,
    payload_json      TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    UNIQUE (category, symbol, timeframe, as_of_ms, engine_version, config_hash, input_fingerprint)
);
CREATE INDEX idx_setup_snapshots_lookup ON setup_snapshots(category, symbol, timeframe, as_of_ms);
CREATE TRIGGER setup_snapshots_immutable BEFORE UPDATE ON setup_snapshots
BEGIN SELECT RAISE(ABORT, 'setup snapshots are immutable'); END;
CREATE TRIGGER setup_snapshots_no_delete BEFORE DELETE ON setup_snapshots
BEGIN SELECT RAISE(ABORT, 'setup snapshots are append-only'); END;

-- Identity of each setup, recorded once (what it IS: type, direction, trigger, level).
CREATE TABLE setups (
    setup_id          TEXT PRIMARY KEY,
    category          TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    timeframe         TEXT NOT NULL,
    setup_type        TEXT NOT NULL CHECK (setup_type IN ('breakout','pullback','sweep_reversal','range_rejection')),
    direction         TEXT NOT NULL CHECK (direction IN ('bullish','bearish')),
    trigger_event_id  TEXT NOT NULL,
    setup_time_ms     INTEGER NOT NULL,
    key_level         REAL NOT NULL,
    engine_version    TEXT NOT NULL,
    config_hash       TEXT NOT NULL,
    first_seen_as_of_ms INTEGER NOT NULL,
    identity_json     TEXT NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE INDEX idx_setups_series ON setups(category, symbol, timeframe, setup_time_ms);
CREATE TRIGGER setups_append_only_u BEFORE UPDATE ON setups
BEGIN SELECT RAISE(ABORT, 'setups are append-only'); END;
CREATE TRIGGER setups_append_only_d BEFORE DELETE ON setups
BEGIN SELECT RAISE(ABORT, 'setups are append-only'); END;

-- Lifecycle log: each status is reached at most once per setup.
CREATE TABLE setup_events (
    event_id          TEXT PRIMARY KEY,
    setup_id          TEXT NOT NULL REFERENCES setups(setup_id),
    status            TEXT NOT NULL CHECK (status IN ('candidate','confirmed','invalidated','expired')),
    at_ms             INTEGER NOT NULL,
    reason            TEXT NOT NULL,
    first_seen_as_of_ms INTEGER NOT NULL,
    created_at        TEXT NOT NULL,
    UNIQUE (setup_id, status)
);
CREATE INDEX idx_setup_events_setup ON setup_events(setup_id, at_ms);
CREATE TRIGGER setup_events_append_only_u BEFORE UPDATE ON setup_events
BEGIN SELECT RAISE(ABORT, 'setup events are append-only'); END;
CREATE TRIGGER setup_events_append_only_d BEFORE DELETE ON setup_events
BEGIN SELECT RAISE(ABORT, 'setup events are append-only'); END;

-- A later evaluation disagreeing with what was recorded (identity or a transition time/reason).
-- The first record stays authoritative; the disagreement is kept, never silently overwritten.
CREATE TABLE setup_conflicts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    setup_id          TEXT NOT NULL,
    seen_as_of_ms     INTEGER NOT NULL,
    kind              TEXT NOT NULL CHECK (kind IN ('identity','transition')),
    detail            TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    UNIQUE (setup_id, seen_as_of_ms, kind, detail)
);
