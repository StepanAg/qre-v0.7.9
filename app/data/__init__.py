"""Market data layer: provider port, Bybit public REST adapter, normalisation,
validation, data-quality checks and backfill orchestration.

Network access is confined to app/data/http.py (enforced by an AST test).
Consumers (research/strategy/backtest) use the MarketDataStore port only."""
