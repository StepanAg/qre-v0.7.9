"""Research layer: Data Quality Policy -> validated time series -> Feature Engine
-> FeatureSnapshot. Pure computation over closed candles.

Never trades, sizes, calls the network, the private API, Telegram or AI.
Reads market data only through the MarketDataStore port."""
