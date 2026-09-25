-- Phase 5: market structure & OHLCV-derived liquidity. Nothing existing is altered.

CREATE TABLE structure_snapshots (
    snapshot_id       TEXT PRIMARY KEY,
    category          TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    timeframe         TEXT NOT NULL CHECK (timeframe IN ('1m','5m','15m','1h','4h','1d')),
    as_of_ms          INTEGER NOT NULL,
    direction         TEXT NOT NULL CHECK (direction IN ('bullish','bearish','unknown')),
    data_quality      TEXT NOT NULL,
    structure_version TEXT NOT NULL,
    config_hash       TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    code_version      TEXT NOT NULL,
    payload_json      TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    UNIQUE (category, symbol, timeframe, as_of_ms, structure_version, config_hash, input_fingerprint),
    CHECK ((data_quality = 'valid') OR (direction = 'unknown'))
);
CREATE INDEX idx_structure_lookup ON structure_snapshots(category, symbol, timeframe, as_of_ms);
CREATE TRIGGER structure_snapshots_immutable BEFORE UPDATE ON structure_snapshots
BEGIN SELECT RAISE(ABORT, 'structure snapshots are immutable'); END;
CREATE TRIGGER structure_snapshots_no_delete BEFORE DELETE ON structure_snapshots
BEGIN SELECT RAISE(ABORT, 'structure snapshots are append-only'); END;

-- Event log: every BOS / CHoCH / unclassified break / sweep, recorded once.
-- event_id identifies WHAT happened (level + bar + method + version + config), not its label,
-- so a later, different classification of the same break can never silently replace the first one.
CREATE TABLE structure_events (
    event_id          TEXT PRIMARY KEY,
    category          TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    timeframe         TEXT NOT NULL,
    kind              TEXT NOT NULL CHECK (kind IN ('bos','choch','break_unclassified','sweep')),
    direction         TEXT NOT NULL,          -- bullish/bearish for breaks, buy_side/sell_side for sweeps
    event_time_ms     INTEGER NOT NULL,
    level_price       REAL NOT NULL,
    structure_version TEXT NOT NULL,
    config_hash       TEXT NOT NULL,
    first_seen_as_of_ms INTEGER NOT NULL,
    payload_json      TEXT NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE INDEX idx_structure_events_series ON structure_events(category, symbol, timeframe, event_time_ms);
CREATE TRIGGER structure_events_append_only_u BEFORE UPDATE ON structure_events
BEGIN SELECT RAISE(ABORT, 'structure events are append-only'); END;
CREATE TRIGGER structure_events_append_only_d BEFORE DELETE ON structure_events
BEGIN SELECT RAISE(ABORT, 'structure events are append-only'); END;

-- Same event identity seen later with a different classification (e.g. a shorter
-- structure window). The first record stays authoritative; the disagreement is kept.
CREATE TABLE structure_event_conflicts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id          TEXT NOT NULL REFERENCES structure_events(event_id),
    seen_as_of_ms     INTEGER NOT NULL,
    existing_kind     TEXT NOT NULL,
    new_kind          TEXT NOT NULL,
    new_payload_json  TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    UNIQUE (event_id, seen_as_of_ms, new_kind)
);
