"""Market Monitor runtime (Phase 4): continuously keeps closed-bar market data,
features and regime snapshots up to date. Orchestration only: it reuses the
Phase 1 ingestion (BackfillService), Phase 2 features and Phase 3 regime engine.

No orders, no private endpoints, no LLM. Never starts on import."""
