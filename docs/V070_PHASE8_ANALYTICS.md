# QRE v0.7.0 — Analytics (Phase 8)

> Read-only analytics over what QRE actually records. It measures; it never trades, writes, corrects or optimises. Research trades are hypothetical test-rule simulations (Phase 7), not real trades. Nothing here proves profitability.

Decisions applied: **D1 = NO** (research trades carry `regime_fit` only; regime labels are `source_not_available` for them), **D2 = YES** (warning at n < 30; p25/p75 from n ≥ 4; p10/p90 from n ≥ 20), **D3 = NO** (no migration; missing append-only triggers on conflict tables are only diagnosed).

## Architecture
```
SQLite ──(mode=ro, SELECT only)──► storage/analytics_read.py ──domain objects──► analytics/engine.py (pure)
                                                                               ► analytics/quality.py
                                                                               ► analytics/report.py ─► cli/analytics_cmds.py
```
| Module | Role |
| --- | --- |
| `app/storage/analytics_read.py` | `SQLiteAnalyticsReader`: own connection opened `mode=ro`, SELECT only (AST test) |
| `app/analytics/ports.py` | `AnalyticsSource` Protocol (storage implements it without importing analytics) |
| `app/analytics/sources.py` | source registry: available / schema_only_no_data / source_not_available |
| `app/analytics/filters.py` | `AnalyticsFilter`; inapplicable filters raise (CLI exit 2) |
| `app/analytics/distributions.py` | D2 sample rules |
| `app/analytics/drawdown.py` | drawdown episodes, durations, curves |
| `app/analytics/excursions.py` | trade MAE/MFE, capture, entry/exit efficiency from candles |
| `app/analytics/engine.py` | performance, risk, costs, exits, groups, portfolio, periods, funnels, untraded, version guards, compare, `AnalyticsEngine` facade |
| `app/analytics/quality.py` | data quality / anomalies, monitor summary |
| `app/analytics/report.py` | JSON / CSV / text rendering |
| `app/research/lab/stats.py` | + `percentile`, `sample_warning` (existing functions unchanged) |
Unchanged on purpose: `app/research/lab/metrics.py` (changing it would make reruns of stored Phase 7 backtests conflict), `app/analytics/metrics.py` and `app/domain/analytics.py` (Phase 0 adapter for future real trades). No tables, no migrations.

## Sources
| Source | Tables | Status |
| --- | --- | --- |
| research.trades / equity / observations / outcomes / runs | sim_trades, sim_equity, replay_observations, outcome_observations, research_runs, research_datasets, metric_reports | available |
| market.candles | candles | available (excursions are computed) |
| live.setups / regime / structure / monitor | setups, setup_events, setup_snapshots, regime_snapshots, structure_*, monitor_* | available |
| integrity.conflicts | setup_conflicts, structure_event_conflicts, research_conflicts | available |
| real.trades | trades, fills, trade_legs | schema_only_no_data (nothing writes them) |
| ai.decisions, risk.decisions, market.quotes (bid/ask), exec.post_exit | — | source_not_available |
Live and research data are never mixed: every answer carries `source` (`live` or `research:<run_id>`).

## Metric rules (summary; formulas in the Phase 8 TZ)
- Format: `{"value": x}` or `{"status": "not_available", "reason": ...}`; text/CSV show `N/A: reason`.
- Breakeven (|net| ≤ 1e-9) is neither a win nor a loss. PF with no losing trades → `no_losing_trades` (never ∞); all-losing PF = 0 is a real value.
- Costs follow the Phase 0 accounting model: `net = gross − fees + funding`; slippage is already inside gross (attribution); `theoretical_gross = gross + slippage`.
- Excursions: window = bars with open_time in [entry, exit); OHLC resolution (exit-bar extremes are an upper bound); gap in the window → `gap_in_trade_window`. Entry efficiency v1 = 1 − MAE/(MAE+MFE); exit efficiency = position of the exit inside the window range; capture = realized move / MFE.
- Filters on a backtest run make equity-derived metrics `equity_not_filterable` (the equity curve covers the whole run).
- Funnel: steps from `skipped_signals`; a step larger than the previous one → `count_exceeds_previous_step` + `FUNNEL_INCONSISTENT` warning.
- Versions: runs whose `versions_json` differ from `component_versions()` are excluded (`version_mismatch`) unless `--include-stale-versions`; mixed live configs → `mixed_configs` unless `--allow-mixed-configs`.
- Monitor staleness is judged from data only: a `running` run is stale when a newer run started after its last heartbeat (no wall clock).
- Stored Phase 7 metric reports count breakeven as a loss; Phase 8 recomputes everything from `sim_trades` (`legacy_metric_semantics` note in the report).

## CLI
```
python -m app analytics summary | data-quality | monitor | ai | rejected-real
python -m app analytics {trades|performance|risk|costs|exits|portfolio|untraded|horizons} --run <bt>
python -m app analytics drawdown --run <bt> [--curve equity|r|drawdown]
python -m app analytics periods  --run <bt> --by day|week|month
python -m app analytics {setups|regimes|funnel} --run <bt> | --live [--allow-mixed-configs]
python -m app analytics compare --runs <bt1> <bt2> [--force-incomparable]
python -m app analytics report  --run <bt> [--out report.json]
filters: --from --to (ISO with TZ) --symbol --direction --setup --regime-fit --regime (live only) --timeframe --exit-reason
output:  --format text|json|csv  --out FILE   ·   --include-stale-versions
```
JSON envelope: `{source, filters, as_of, versions, warnings, data}`; `as_of` is the latest data timestamp used, never "now". CSV only for flat outputs (trades, periods, groups, exits, funnel steps, data-quality); nested outputs return exit code 2 with a hint.

## Limitations
OHLC resolution for MAE/MFE, no bid/ask; no regime label for research trades (D1); no margin/leverage model; no Horizon or Entry Zone engine (planned horizon, late entry → not available); no AI, risk decisions or real trades; conflict tables lack append-only triggers (diagnosed, D3); statistics with n < 30 carry a warning.
