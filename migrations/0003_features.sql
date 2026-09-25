-- Phase 2: feature snapshots. Separate from market-data tables.
-- Normalised (one row per feature) rather than JSON: every value carries its own
-- status/quality/version and can be queried and constrained individually.

CREATE TABLE feature_snapshots (
    snapshot_id       TEXT PRIMARY KEY,
    category          TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    timeframe         TEXT NOT NULL CHECK (timeframe IN ('1m','5m','15m','1h','4h','1d')),
    as_of_ms          INTEGER NOT NULL,          -- decision time; only bars closed at as_of were used
    feature_set_hash  TEXT NOT NULL,             -- names + versions + formula hashes
    input_fingerprint TEXT NOT NULL,             -- exact input bars
    engine_version    TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    UNIQUE (category, symbol, timeframe, as_of_ms, feature_set_hash, input_fingerprint)
);
CREATE INDEX idx_fs_lookup ON feature_snapshots(category, symbol, timeframe, as_of_ms);

CREATE TABLE feature_values (
    snapshot_id       TEXT NOT NULL REFERENCES feature_snapshots(snapshot_id),
    feature_key       TEXT NOT NULL,             -- "ema_200" or "1h:ema_200"
    name              TEXT NOT NULL,
    timeframe         TEXT NOT NULL,
    version           INTEGER NOT NULL,
    status            TEXT NOT NULL CHECK (status IN
                        ('value','not_available','insufficient_history','data_quality_failure','invalid')),
    value             REAL,                      -- IEEE double, exact round-trip
    unit              TEXT NOT NULL,
    lookback          INTEGER NOT NULL,
    min_observations  INTEGER NOT NULL,
    bars_used         INTEGER NOT NULL,
    quality           TEXT NOT NULL,
    gap_severity      TEXT NOT NULL,
    reason            TEXT,
    PRIMARY KEY (snapshot_id, feature_key),
    -- a value exists iff status = 'value'; never NaN (SQLite stores NaN as NULL)
    CHECK ((status = 'value') = (value IS NOT NULL)),
    CHECK (status = 'value' OR reason IS NOT NULL)
) WITHOUT ROWID;

CREATE TRIGGER feature_values_immutable BEFORE UPDATE ON feature_values
BEGIN SELECT RAISE(ABORT, 'feature values are immutable'); END;
CREATE TRIGGER feature_snapshots_immutable BEFORE UPDATE ON feature_snapshots
BEGIN SELECT RAISE(ABORT, 'feature snapshots are immutable'); END;
