-- Phase 1: market data.
-- Times are INTEGER epoch milliseconds (UTC). Decimals are TEXT.
-- The candles table holds CLOSED candles only; open candles are never persisted.

CREATE TABLE instruments (
    category          TEXT NOT NULL CHECK (category IN ('linear','spot')),
    symbol            TEXT NOT NULL,
    base_coin         TEXT,
    quote_coin        TEXT,
    settle_coin       TEXT,
    status            TEXT,
    contract_type     TEXT,
    tick_size         TEXT NOT NULL,
    qty_step          TEXT NOT NULL,
    min_qty           TEXT NOT NULL,
    min_notional      TEXT,
    max_qty           TEXT,
    max_market_qty    TEXT,
    price_scale       INTEGER,
    min_leverage      TEXT,
    max_leverage      TEXT,
    leverage_step     TEXT,
    funding_interval_min INTEGER,
    launch_time_ms    INTEGER,
    source            TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (category, symbol)
);

-- PK doubles as the (symbol, timeframe, time) index and the uniqueness guarantee.
CREATE TABLE candles (
    category          TEXT NOT NULL CHECK (category IN ('linear','spot')),
    symbol            TEXT NOT NULL,
    timeframe         TEXT NOT NULL CHECK (timeframe IN ('1m','5m','15m','1h','4h','1d')),
    open_time_ms      INTEGER NOT NULL,
    open              TEXT NOT NULL,
    high              TEXT NOT NULL,
    low               TEXT NOT NULL,
    close             TEXT NOT NULL,
    volume            TEXT NOT NULL,   -- base coin quantity
    turnover          TEXT NOT NULL,   -- quote currency notional
    source            TEXT NOT NULL,
    ingested_at       TEXT NOT NULL,
    PRIMARY KEY (category, symbol, timeframe, open_time_ms)
) WITHOUT ROWID;

CREATE TRIGGER candles_aligned BEFORE INSERT ON candles
WHEN NEW.open_time_ms % (CASE NEW.timeframe
        WHEN '1m' THEN 60000 WHEN '5m' THEN 300000 WHEN '15m' THEN 900000
        WHEN '1h' THEN 3600000 WHEN '4h' THEN 14400000 WHEN '1d' THEN 86400000 END) != 0
BEGIN
    SELECT RAISE(ABORT, 'candle open_time not aligned to timeframe');
END;

CREATE TRIGGER candles_immutable BEFORE UPDATE ON candles
BEGIN
    SELECT RAISE(ABORT, 'closed candles are immutable');
END;

CREATE TABLE funding_rates (
    category          TEXT NOT NULL CHECK (category = 'linear'),
    symbol            TEXT NOT NULL,
    funding_time_ms   INTEGER NOT NULL,
    rate              TEXT NOT NULL,   -- fraction per funding interval (0.0001 = 0.01 %)
    source            TEXT NOT NULL,
    ingested_at       TEXT NOT NULL,
    PRIMARY KEY (category, symbol, funding_time_ms)
) WITHOUT ROWID;

CREATE TRIGGER funding_immutable BEFORE UPDATE ON funding_rates
BEGIN
    SELECT RAISE(ABORT, 'funding rates are immutable');
END;

CREATE TABLE backfill_runs (
    run_id            TEXT PRIMARY KEY,
    dataset           TEXT NOT NULL CHECK (dataset IN ('candles','funding')),
    category          TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    timeframe         TEXT,
    range_start_ms    INTEGER NOT NULL,
    range_end_ms      INTEGER NOT NULL,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    status            TEXT NOT NULL,
    pages             INTEGER NOT NULL DEFAULT 0,
    fetched           INTEGER NOT NULL DEFAULT 0,
    inserted          INTEGER NOT NULL DEFAULT 0,
    unchanged         INTEGER NOT NULL DEFAULT 0,
    conflicts         INTEGER NOT NULL DEFAULT 0,
    invalid           INTEGER NOT NULL DEFAULT 0,
    open_excluded     INTEGER NOT NULL DEFAULT 0,
    gaps              INTEGER NOT NULL DEFAULT 0,
    error             TEXT
);

CREATE TABLE data_quality_issues (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT NOT NULL REFERENCES backfill_runs(run_id),
    kind              TEXT NOT NULL,
    at_ms             INTEGER,
    detail            TEXT NOT NULL,
    raw               TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_dq_run ON data_quality_issues(run_id);
