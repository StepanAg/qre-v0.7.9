# QRE v0.7.0 — Strategy + Risk Engine (Phase 9, offline)

> Strategy decides whether a setup is an admissible trade candidate and in which direction. The Risk Engine decides whether that candidate may be traded under the configured limits and how large it may be. Neither executes anything. Everything runs offline over research replays; nothing here proves profitability.

## Pipeline
```
replay_observations (Phase 7) ─► SetupView (point-in-time) ─► StrategyV1 ─► StrategyDecision
                               ─► RiskEngineV1 (+ decision price, PortfolioState, InstrumentRules) ─► RiskDecision (+ RiskPlan)
                               ─► DecisionBacktestEngine (Phase 7 fill/exit/cost model, unchanged) ─► research run ─► analytics
```
| Module | Role |
| --- | --- |
| `app/domain/decision.py` | new: `SetupView`, `setup_view()`, `StrategyDecision`, `PortfolioState`, `OpenExposure`, `InstrumentRules`, `RiskCheck`, `RiskDecision`, `GateResult`; unchanged: `RiskPlan`, `EntryPlan`; deprecated (kept): `Signal`, `Setup` |
| `app/strategy/{config,engine,ports,pipeline,version}.py` | Strategy v1, port, `StrategyRiskGate`, rules lock |
| `app/risk/{config,engine,ports,version}.py` | Risk Engine v1 (first consumer of `RiskSettings`), port, rules lock |
| `app/research/lab/ports.py` | `DecisionGate` Protocol with `evaluate(...)` (research never imports strategy/risk; the identifier `decide` stays forbidden in research by the Phase 2 safety rule) |
| `app/research/lab/backtest.py` | + `DecisionBacktestEngine` (subclass; the v1 code is untouched) |
| `app/research/lab/pipeline.py` | + `ResearchPipeline.decide()` |
| `app/research/lab/config.py` | + `DecisionRunConfig` (`config/decision_run_v1.json`) |
| `app/analytics/engine.py` | + funnel for Strategy + Risk runs (Phase 7 runs unchanged) |
| `app/cli/research_cmds.py` | + `research decide`, `research decisions` |

## Point-in-time
A `SetupView` holds only what was known at the decision close: transitions with `at <= decision_time`, the evidence frozen at creation, `first_regime_fit`, and opposite-direction setups **active at that moment**. The replay observation's own `contradictions` and final `regime_fit` are computed at `last_seen` and are never used (look-ahead). Decisions at the same instant are taken in (symbol, setup_id) order, each seeing earlier approvals of that instant.

## Strategy v1 (`config/strategy_v1.json`)
Rules in order; the first failing rule decides: type enabled (`strategy:unsupported_setup`) → setup active and `entry_on` fact reached (`strategy:invalid_setup`) → regime fit allowed (`strategy:invalid_setup`) → ATR and invalidation reference present (`strategy:missing_required_context`) → no conflicting active setup, if configured (`strategy:conflicting_setup`) → `strategy:accepted` with direction, stop reference (invalidation reference ∓ `stop_buffer_atr`·ATR), take-profit R and max hold. The invalidation references and their arithmetic mirror the Phase 7 test rule exactly (criterion E). Strategy never checks the stop side: it has no price.

## Risk Engine v1 (mode `equity_pct`, default)
1. Global stops: hard drawdown ≥ `HARD_DRAWDOWN_LIMIT_PCT` → **HALTED** (`risk:hard_drawdown_halt`, latched); kill switch ≥ `KILL_SWITCH_DRAWDOWN_PCT` → `risk:kill_switch_active` (latched); daily realized loss ≥ `DAILY_LOSS_LIMIT_PCT` of day-start equity → `risk:daily_loss_limit` (until next UTC day); open positions ≥ `MAX_OPEN_POSITIONS` → `risk:max_open_positions`. Open positions are never closed.
2. Stop side against the decision price → `risk:stop_on_wrong_side` (both modes).
3. Instrument rules required → `risk:instrument_rules_unavailable`.
4. qty = equity × `RISK_PER_TRADE_PCT` / |price − stop|, rounded down to `qty_step`.
5. Leverage cap min(`MAX_LEVERAGE`, instrument max) × equity → size **reduced** (`risk:reduced_leverage_cap`).
6. Minimums **after** every reduction → `risk:below_min_size`; risk is never increased.
7. Open risk + new risk > `MAX_OPEN_RISK_PCT` × equity → **REJECTED** `risk:open_risk_limit` (no reduction).
8. `APPROVED` with the unchanged Phase 0 `RiskPlan` (`planned_entry` = decision price, frozen `initial_stop`).

`fixed_quote` exists only for compatibility: stop-side check, then qty = fixed quote / |price − stop| without rounding or other checks; instrument rules, if present, are recorded as diagnostics and never applied (D9-3). Rejected and halted decisions report qty 0; the evaluated size stays in the checks.

## Compatibility with Phase 7 (criteria D, E)
`BacktestConfig` v1, its code path, `component_versions()`, dataset ids and all stored run ids/result hashes are unchanged (locked by a test with identities recorded before Phase 9). Strategy v1 + Risk `fixed_quote` reproduces the Phase 7 v1 backtest trade for trade (all fields except the run id) and the equity curve on seven fixture scenarios, including fees, slippage and assumed funding. Skip labels differ by namespace only (`risk:stop_on_wrong_side` vs `stop_on_wrong_side`).

## Storage (no migration)
A Strategy + Risk run is a `research_runs` row with `kind = backtest` and `config.pipeline = "strategy_risk_v1"`; its config freezes the Strategy config and rules hash, the Risk config (values of `RiskSettings`) and rules hash, the instrument rules used, and the execution config — all part of the run id. Trades and equity use `sim_trades`/`sim_equity`; per-setup decisions are stored in `metric_reports` (kind `decisions`); counts in `summary.skipped_signals` with `strategy:` / `risk:` prefixes.

## CLI
```
python -m app data instruments                          # instrument rules, needed by equity_pct
python -m app research decide --replay <rp> [--risk-mode equity_pct|fixed_quote --fixed-risk-quote 100]
python -m app research decisions --id <bt>
python -m app analytics funnel --run <bt>               # observed → decidable → evaluated → strategy_accepted → risk_approved → entered → profitable
```

## Limitations
Offline only (no paper/live, no orders, no position closing); regime label not available to Strategy (D1); structure direction at the decision instant is not in the observation; one position per symbol (Phase 7 simulator); portfolio equity is the simulation's MTM equity; instrument rules are taken from the local `instruments` table at run time and frozen into the run; validated on fixtures only (Bybit not reachable here). Technical debt kept: F4 (features referenced by name), F6 (Phase 0 repositories bypass the writer), F7 (symbols/timeframes in four places), F8 (`alignment.py`).
