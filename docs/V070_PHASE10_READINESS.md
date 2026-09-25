# Phase 10 Readiness

Phase 9 delivers an offline, deterministic decision chain: canonical Phase 6 setup → point-in-time SetupView → Strategy v1 → Risk Engine v1 → RiskDecision with an unchanged RiskPlan, evaluated through the Phase 7 simulator and visible in analytics.

## What is available
| Question | Where |
| --- | --- |
| Which setups a rule accepts and why others are rejected | `research decisions --id <bt>`, `analytics funnel --run <bt>` |
| Which risk limits bind (size reductions, open-risk, daily loss, drawdown halts) | risk decisions in `metric_reports` (kind `decisions`) |
| How the equity-based sizing performs vs the fixed-quote Phase 7 rule | two runs of the same replay, `analytics compare` |
| Proof the decision path matches Phase 7 when configured identically | `tests/test_phase9.EquivalenceTests` |

## Open before any execution-related phase
1. Paper/demo execution needs: an execution gateway behind `OrderPermission`, reconciliation, Phase 0 repositories moved behind the single writer (F6), feature versions pinned in live analytics (F4).
2. Instrument rules must be refreshed from Bybit (`data instruments`) on the target machine.
3. Empirical validation on real history remains deferred to v1.0.0 (end-to-end run on the user's machine).
4. Technical debt F7 (symbol/timeframe configuration in four places) and F8 (`alignment.py`).

## Verdict
READY for offline research of Strategy + Risk configurations. NOT READY for paper or live trading: no execution path exists by design.
