-- QRE v0.7.0 initial schema.
-- Principles: decimals as TEXT (no float), append-only facts, exchange ids UNIQUE,
-- no stored PnL columns (PnL is derived from trade_legs + funding_events).

CREATE TABLE signals (
    signal_id         TEXT PRIMARY KEY,
    symbol            TEXT NOT NULL,
    category          TEXT NOT NULL,
    direction         TEXT NOT NULL,
    strategy_id       TEXT NOT NULL,
    strategy_version  TEXT NOT NULL,
    strength          TEXT NOT NULL,
    research_snapshot_id TEXT,
    created_at        TEXT NOT NULL,
    payload_json      TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE trades (
    trade_id          TEXT PRIMARY KEY,
    symbol            TEXT NOT NULL,
    category          TEXT NOT NULL,
    side              TEXT NOT NULL CHECK (side IN ('long','short')),
    signal_id         TEXT REFERENCES signals(signal_id),
    risk_plan_id      TEXT NOT NULL,
    planned_entry     TEXT NOT NULL,
    initial_stop      TEXT NOT NULL,          -- frozen R anchor, never updated
    planned_qty       TEXT NOT NULL,
    exit_reason       TEXT,
    created_at        TEXT NOT NULL,
    reconciliation_status TEXT NOT NULL DEFAULT 'pending'
);

CREATE TRIGGER trades_initial_stop_immutable
BEFORE UPDATE OF initial_stop, planned_entry, planned_qty ON trades
BEGIN
    SELECT RAISE(ABORT, 'initial risk fields are immutable');
END;

CREATE TABLE orders (
    local_order_id    TEXT PRIMARY KEY,
    client_order_id   TEXT NOT NULL UNIQUE,
    exchange_order_id TEXT UNIQUE,
    trade_id          TEXT REFERENCES trades(trade_id),
    signal_id         TEXT REFERENCES signals(signal_id),
    symbol            TEXT NOT NULL,
    category          TEXT NOT NULL,
    side              TEXT NOT NULL,
    order_type        TEXT NOT NULL,
    time_in_force     TEXT NOT NULL,
    qty               TEXT NOT NULL,
    price             TEXT,
    reduce_only       INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL,
    source            TEXT NOT NULL,
    reconciliation_status TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX idx_orders_status ON orders(status);

CREATE TABLE order_events (
    event_id          TEXT PRIMARY KEY,
    local_order_id    TEXT NOT NULL REFERENCES orders(local_order_id),
    status            TEXT NOT NULL,
    source            TEXT NOT NULL,
    occurred_at       TEXT NOT NULL,
    recorded_at       TEXT NOT NULL,
    idempotency_key   TEXT NOT NULL UNIQUE,
    detail            TEXT NOT NULL DEFAULT ''
);

-- Fills are facts. exec_id (exchange execution id) is the dedup key.
CREATE TABLE fills (
    exec_id           TEXT PRIMARY KEY,
    local_order_id    TEXT REFERENCES orders(local_order_id),
    exchange_order_id TEXT,
    symbol            TEXT NOT NULL,
    category          TEXT NOT NULL,
    side              TEXT NOT NULL,
    qty               TEXT NOT NULL,
    price             TEXT NOT NULL,
    fee               TEXT NOT NULL,
    fee_asset         TEXT NOT NULL,
    is_maker          INTEGER NOT NULL,
    expected_price    TEXT,
    exec_time         TEXT NOT NULL,
    source            TEXT NOT NULL,
    recorded_at       TEXT NOT NULL
);
CREATE TRIGGER fills_append_only_upd BEFORE UPDATE ON fills
BEGIN SELECT RAISE(ABORT, 'fills are append-only'); END;
CREATE TRIGGER fills_append_only_del BEFORE DELETE ON fills
BEGIN SELECT RAISE(ABORT, 'fills are append-only'); END;

-- A fill belongs to at most one trade.
CREATE TABLE trade_legs (
    trade_id          TEXT NOT NULL REFERENCES trades(trade_id),
    exec_id           TEXT NOT NULL UNIQUE REFERENCES fills(exec_id),
    role              TEXT NOT NULL CHECK (role IN ('entry','exit')),
    PRIMARY KEY (trade_id, exec_id)
);

CREATE TABLE funding_events (
    funding_id        TEXT PRIMARY KEY,
    exchange_ref      TEXT NOT NULL UNIQUE,
    trade_id          TEXT REFERENCES trades(trade_id),
    symbol            TEXT NOT NULL,
    amount            TEXT NOT NULL,
    asset             TEXT NOT NULL,
    rate              TEXT,
    occurred_at       TEXT NOT NULL
);

CREATE TABLE position_events (
    event_id          TEXT PRIMARY KEY,
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL,
    event_type        TEXT NOT NULL,
    qty_delta         TEXT NOT NULL,
    price             TEXT NOT NULL,
    exec_id           TEXT UNIQUE REFERENCES fills(exec_id),
    trade_id          TEXT REFERENCES trades(trade_id),
    source            TEXT NOT NULL,
    occurred_at       TEXT NOT NULL
);

-- Raw exchange state kept separately from local state.
CREATE TABLE exchange_snapshots (
    snapshot_id       TEXT PRIMARY KEY,
    kind              TEXT NOT NULL,           -- positions|open_orders|wallet
    taken_at          TEXT NOT NULL,
    payload_json      TEXT NOT NULL
);

CREATE TABLE account_snapshots (
    snapshot_id       TEXT PRIMARY KEY,
    source            TEXT NOT NULL,
    taken_at          TEXT NOT NULL,
    asset             TEXT NOT NULL,
    equity            TEXT NOT NULL,
    wallet_balance    TEXT NOT NULL,
    available_balance TEXT NOT NULL,
    unrealized_pnl    TEXT NOT NULL
);

CREATE TABLE reconciliation_runs (
    run_id            TEXT PRIMARY KEY,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    trigger           TEXT NOT NULL,           -- startup|periodic|manual
    result            TEXT,                    -- matched|mismatch|error
    details_json      TEXT NOT NULL DEFAULT '{}'
);

-- Generic append-only journal for any domain event.
CREATE TABLE event_log (
    seq               INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key   TEXT NOT NULL UNIQUE,
    event_type        TEXT NOT NULL,
    aggregate_id      TEXT NOT NULL,
    correlation_id    TEXT,
    occurred_at       TEXT NOT NULL,
    recorded_at       TEXT NOT NULL,
    payload_json      TEXT NOT NULL
);
CREATE INDEX idx_event_log_aggregate ON event_log(aggregate_id);
