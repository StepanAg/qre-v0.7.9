# QRE — Quantitative Research & Execution Engine

Version **0.7.0** (target 1.0.0) · Phase 9: offline Strategy + Risk Engine. Implemented: market data, data quality & features, regime, monitor + supervisor, market structure, setup detection, research & backtest infrastructure, analytics, Strategy v1 and Risk Engine v1 (offline only). No live trading, no execution, no AI.

```bash
python test.py            # unified test runner (offline: network is blocked)
python test.py --network  # + real Bybit public API smoke test
python -m app data backfill --symbols BTCUSDT --tf 15m --days 30
python -m app monitor run --hours 2   # continuous regime monitor (Ctrl+C = graceful stop)
python -m app monitor health
python -m app structure analyze --symbol BTCUSDT --tf 1h   # swings, BOS/CHoCH, EQH/EQL, liquidity
python -m app setup analyze --symbol BTCUSDT --tf 1h       # breakout / pullback / sweep reversal / range rejection
python -m app research dataset build --symbols BTCUSDT --tf 15m --start ... --end ...   # then: replay, outcomes, backtest
python -m app analytics report --run <backtest run id>     # read-only analytics; see docs/V070_PHASE8_ANALYTICS.md
python -m app research decide --replay <replay run id>      # Strategy -> Risk Engine, offline; see docs/V070_PHASE9_STRATEGY_RISK.md
python -m app features snapshot --symbol BTCUSDT --tf 15m --mtf 1h 4h
python -m app --help      # version | config | status | test | db-init
cp .env.example .env      # optional; defaults are safe
```

Safety: execution mode `disabled`, live/spot/demo flags `false`, build ceiling `paper`.
There is no code path that can place an exchange order in this build.

Docs: `docs/V070_*.md` — architecture, domain model, lessons learned, testing, market data, schema, data quality, Phase 2 readiness.
