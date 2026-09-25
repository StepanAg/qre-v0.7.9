-- Phase 7: research & backtest. Separate tables; nothing existing is altered.
-- Simulated trades live ONLY in sim_trades, never in the real `trades` table.

CREATE TABLE research_datasets (
    dataset_id        TEXT PRIMARY KEY,           -- hash(spec + component versions + configs + data fingerprints)
    spec_json         TEXT NOT NULL,
    manifest_json     TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

CREATE TABLE research_runs (
    run_id            TEXT PRIMARY KEY,           -- deterministic: hash(parent + config)
    kind              TEXT NOT NULL CHECK (kind IN ('replay','outcomes','backtest','splits')),
    parent_id         TEXT NOT NULL,              -- dataset_id (replay) or replay run (outcomes/backtest/splits)
    dataset_id        TEXT NOT NULL REFERENCES research_datasets(dataset_id),
    config_json       TEXT NOT NULL,
    versions_json     TEXT NOT NULL,
    summary_json      TEXT NOT NULL,
    result_hash       TEXT NOT NULL,              -- hash of the full result: a rerun must reproduce it
    created_at        TEXT NOT NULL
);
CREATE INDEX idx_research_runs_parent ON research_runs(parent_id, kind);

CREATE TABLE replay_observations (
    run_id            TEXT NOT NULL REFERENCES research_runs(run_id),
    setup_id          TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    setup_time_ms     INTEGER NOT NULL,
    payload_json      TEXT NOT NULL,
    PRIMARY KEY (run_id, setup_id)
);

CREATE TABLE outcome_observations (
    run_id            TEXT NOT NULL REFERENCES research_runs(run_id),
    setup_id          TEXT NOT NULL,
    horizon           INTEGER NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('valid','censored','incomplete','no_anchor')),
    payload_json      TEXT NOT NULL,
    PRIMARY KEY (run_id, setup_id, horizon)
);

CREATE TABLE sim_trades (
    run_id            TEXT NOT NULL REFERENCES research_runs(run_id),
    trade_no          INTEGER NOT NULL,
    dataset_id        TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    setup_id          TEXT NOT NULL,
    entry_time_ms     INTEGER NOT NULL,
    exit_time_ms      INTEGER NOT NULL,
    net_pnl           REAL NOT NULL,
    payload_json      TEXT NOT NULL,
    PRIMARY KEY (run_id, trade_no)
);

CREATE TABLE sim_equity (
    run_id            TEXT NOT NULL REFERENCES research_runs(run_id),
    ts_ms             INTEGER NOT NULL,
    equity            REAL NOT NULL,
    PRIMARY KEY (run_id, ts_ms)
);

CREATE TABLE metric_reports (
    report_id         TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL REFERENCES research_runs(run_id),
    kind              TEXT NOT NULL,
    report_json       TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

CREATE TABLE research_conflicts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT NOT NULL,
    stored_hash       TEXT NOT NULL,
    new_hash          TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

-- completed research results are immutable
CREATE TRIGGER research_datasets_ro_u BEFORE UPDATE ON research_datasets BEGIN SELECT RAISE(ABORT, 'research results are immutable'); END;
CREATE TRIGGER research_datasets_ro_d BEFORE DELETE ON research_datasets BEGIN SELECT RAISE(ABORT, 'research results are immutable'); END;
CREATE TRIGGER research_runs_ro_u BEFORE UPDATE ON research_runs BEGIN SELECT RAISE(ABORT, 'research results are immutable'); END;
CREATE TRIGGER research_runs_ro_d BEFORE DELETE ON research_runs BEGIN SELECT RAISE(ABORT, 'research results are immutable'); END;
CREATE TRIGGER replay_obs_ro_u BEFORE UPDATE ON replay_observations BEGIN SELECT RAISE(ABORT, 'research results are immutable'); END;
CREATE TRIGGER replay_obs_ro_d BEFORE DELETE ON replay_observations BEGIN SELECT RAISE(ABORT, 'research results are immutable'); END;
CREATE TRIGGER outcome_obs_ro_u BEFORE UPDATE ON outcome_observations BEGIN SELECT RAISE(ABORT, 'research results are immutable'); END;
CREATE TRIGGER outcome_obs_ro_d BEFORE DELETE ON outcome_observations BEGIN SELECT RAISE(ABORT, 'research results are immutable'); END;
CREATE TRIGGER sim_trades_ro_u BEFORE UPDATE ON sim_trades BEGIN SELECT RAISE(ABORT, 'research results are immutable'); END;
CREATE TRIGGER sim_trades_ro_d BEFORE DELETE ON sim_trades BEGIN SELECT RAISE(ABORT, 'research results are immutable'); END;
CREATE TRIGGER sim_equity_ro_u BEFORE UPDATE ON sim_equity BEGIN SELECT RAISE(ABORT, 'research results are immutable'); END;
CREATE TRIGGER sim_equity_ro_d BEFORE DELETE ON sim_equity BEGIN SELECT RAISE(ABORT, 'research results are immutable'); END;
CREATE TRIGGER metric_reports_ro_u BEFORE UPDATE ON metric_reports BEGIN SELECT RAISE(ABORT, 'research results are immutable'); END;
CREATE TRIGGER metric_reports_ro_d BEFORE DELETE ON metric_reports BEGIN SELECT RAISE(ABORT, 'research results are immutable'); END;
