"""StructureService: stored closed candles -> DataQualityPolicy -> StructureEngine.
ATR for equal highs/lows comes from the Phase 2 Feature Engine evaluated
point-in-time (series.upto(t)), never from a later bar. No network."""
from __future__ import annotations

from datetime import datetime

from app.data.ports import MarketDataStore
from app.domain.market import Symbol, Timeframe
from app.domain.structure import StructureSnapshot
from app.research.engine import FeatureEngine
from app.research.features.catalog import DEFAULT_REGISTRY
from app.research.ports import StructureSnapshotStore
from app.research.quality import DataQualityPolicy
from app.research.structure.config import StructureConfig
from app.research.structure.engine import StructureEngine

_STALE_PROBE = 1   # one extra bar so a missing latest bar is reported as STALE (same convention as Phase 2)


class StructureService:
    def __init__(self, market: MarketDataStore, config: StructureConfig,
                 store: StructureSnapshotStore | None = None) -> None:
        self.market = market
        self.config = config
        self.store = store
        self.policy = DataQualityPolicy()
        self.engine = StructureEngine(config, self.policy)
        self.features = FeatureEngine(policy=self.policy)
        self.atr_spec = DEFAULT_REGISTRY.get(config.atr_feature)

    def bars_to_load(self) -> int:
        """Structure window + the history the ATR needs at the oldest pivot."""
        return self.config.window_bars + self.atr_spec.min_observations + _STALE_PROBE

    def compute(self, candles, symbol: Symbol, timeframe: Timeframe, as_of: datetime) -> StructureSnapshot:
        """Pure path used by tests and the service: candles known at as_of only."""
        as_of = timeframe.floor(as_of)
        series = self.policy.prepare(candles, timeframe, as_of, symbol)
        return self.engine.compute(series, lambda t: self.features.evaluate(series.upto(t), self.atr_spec))

    def analyze(self, symbol: Symbol, timeframe: Timeframe, as_of: datetime, *, save: bool = False
                ) -> StructureSnapshot:
        as_of = timeframe.floor(as_of)
        candles = self.market.last_candles(symbol, timeframe, as_of, self.bars_to_load())
        snap = self.compute(candles, symbol, timeframe, as_of)
        if save and self.store is not None:
            self.store.save(snap)
        return snap
