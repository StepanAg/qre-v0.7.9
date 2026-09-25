-- Phase 3: regime snapshots. Separate table; nothing existing is altered.
-- One row = one immutable classification. The full explanation (per-timeframe
-- metrics, reason codes, BTC context) is kept as canonical JSON in payload_json,
-- the most queried fields are also columns.

CREATE TABLE regime_snapshots (
    snapshot_id       TEXT PRIMARY KEY,
    category          TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    timeframe         TEXT NOT NULL CHECK (timeframe IN ('1m','5m','15m','1h','4h','1d')),
    as_of_ms          INTEGER NOT NULL,
    regime            TEXT NOT NULL CHECK (regime IN
                        ('trending_up','trending_down','ranging','high_volatility','transition','unknown')),
    trend_direction   TEXT NOT NULL,
    trend_strength    TEXT NOT NULL,
    volatility_state  TEXT NOT NULL,
    data_quality      TEXT NOT NULL,
    mtf_alignment     TEXT NOT NULL,
    btc_status        TEXT,
    btc_regime        TEXT,
    regime_version    TEXT NOT NULL,
    config_hash       TEXT NOT NULL,
    feature_version   TEXT NOT NULL,
    code_version      TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    payload_json      TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    -- same market data + same rules + same thresholds = same snapshot
    UNIQUE (category, symbol, timeframe, as_of_ms, regime_version, config_hash, input_fingerprint),
    CHECK ((regime = 'unknown') OR (data_quality = 'valid'))
);
CREATE INDEX idx_regime_lookup ON regime_snapshots(category, symbol, timeframe, as_of_ms);

CREATE TRIGGER regime_snapshots_immutable BEFORE UPDATE ON regime_snapshots
BEGIN SELECT RAISE(ABORT, 'regime snapshots are immutable'); END;
CREATE TRIGGER regime_snapshots_no_delete BEFORE DELETE ON regime_snapshots
BEGIN SELECT RAISE(ABORT, 'regime snapshots are append-only'); END;
