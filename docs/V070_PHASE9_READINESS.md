# Phase 9 Readiness

Phase 8 makes QRE able to measure its own recorded behaviour — research runs and live data — read-only, reproducibly and without inventing numbers. It does not make QRE ready to trade.

## What is now available
| Question | Command | Source |
| --- | --- | --- |
| How did a hypothetical rule perform, net of assumed costs? | `analytics performance / risk / costs --run <bt>` | sim_trades, sim_equity |
| How deep and long were drawdowns? | `analytics drawdown --run <bt> [--curve ...]` | sim_equity |
| Did price go against the entry first; how much of the move was captured? | `analytics trades / exits --run <bt>` | sim_trades + candles |
| Which setup types, directions, regime fits, holding buckets carry the result? | `analytics setups / regimes / horizons --run <bt>` | sim_trades + replay_observations |
| Where do setups drop out before a simulated entry? | `analytics funnel --run <bt>` | research_runs.summary_json |
| What happened after setups the rule did not trade? | `analytics untraded --run <bt>` (POST_SETUP_ANALYSIS) | outcome_observations |
| How do live setups progress and which regimes produce them? | `analytics funnel / setups / regimes --live` | setups, setup_events, regime_snapshots |
| Is the recorded data consistent? | `analytics data-quality` | all tables |

## Manual checks before anything else
1. Load months of real data (`data backfill`, `data funding`), run `research dataset build → replay → outcomes → backtest`.
2. `python -m app analytics report --run <bt> --format json --out report.json` and `analytics data-quality`: expect 0 critical findings.
3. Read results only on the chronological validation split and only where no sample-size warning is shown.

## Open items carried forward
- No regime label for research trades (decision D1). Revisit only together with a replay payload version in the replay run id, otherwise reruns of stored replays would conflict.
- Conflict tables have no append-only triggers (decision D3): reported by data-quality, not enforced.
- Stored Phase 7 metric reports count breakeven as a loss; analytics recomputes from sim_trades.
- No margin/leverage model, no Entry Zone or Horizon engine, no AI layer, no real trades: the corresponding analytics stay `source_not_available`.

## Verdict
READY to measure real research runs and live monitor data. NOT READY for trading decisions: there is no Risk Engine, no execution path and no validated edge.
