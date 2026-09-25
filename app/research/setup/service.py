"""SetupService: stored closed candles -> DataQualityPolicy -> per-bar Structure
(Phase 5, point-in-time) + ATR (Phase 2) -> SetupEngine. No network, no SQL.

structure_at(t) is StructureEngine.compute(series.upto(t)) - exactly what the
monitor computes live at t. Results are cached by the input fingerprint of the
bars they depend on, so a monitor re-evaluating every bar recomputes one bar,
not the whole replay, and a cache hit is guaranteed to be identical."""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone

from app.data.ports import MarketDataStore
from app.domain.market import Symbol, Timeframe
from app.domain.regime import RegimeSnapshot
from app.domain.setup import SetupSnapshot
from app.domain.structure import StructureSnapshot
from app.research.engine import FeatureEngine, input_fingerprint
from app.research.features.catalog import DEFAULT_REGISTRY
from app.research.ports import SetupSnapshotStore
from app.research.quality import DataQualityPolicy, ValidatedSeries
from app.research.setup.config import SetupConfig
from app.research.setup.engine import SetupEngine
from app.research.structure.service import StructureService

_CACHE_SIZE = 4096      # structure snapshots kept in memory (~10 KB each)


class SetupService:
    def __init__(self, market: MarketDataStore | None, structure: StructureService, config: SetupConfig,
                 store: SetupSnapshotStore | None = None) -> None:
        self.market = market
        self.structure = structure
        self.config = config
        self.store = store
        self.policy = DataQualityPolicy()
        self.engine = SetupEngine(config, structure.config.min_bars, self.policy)
        self.features = FeatureEngine(policy=self.policy)
        self.atr_spec = DEFAULT_REGISTRY.get(config.atr_feature)
        self._cache: OrderedDict[tuple, StructureSnapshot] = OrderedDict()
        self.cache_hits = 0
        self.cache_misses = 0

    def bars_to_load(self) -> int:
        return self.structure.bars_to_load() + self.config.lookback_bars

    def _structure_at(self, series: ValidatedSeries, t: datetime) -> StructureSnapshot:
        sub = series.upto(t)
        load = self.structure.bars_to_load()
        key = (series.symbol, series.timeframe, t, self.structure.config.config_hash,
               input_fingerprint([(sub, min(len(sub), load))]))
        hit = self._cache.get(key)
        if hit is not None:
            self.cache_hits += 1
            self._cache.move_to_end(key)
            return hit
        self.cache_misses += 1
        tail = sub.tail(load)            # exactly the bars StructureService.analyze(t) would load
        snap = self.structure.engine.compute(tail, lambda x: self.features.evaluate(tail.upto(x),
                                                                                  self.structure.atr_spec))
        self._cache[key] = snap
        if len(self._cache) > _CACHE_SIZE:
            self._cache.popitem(last=False)
        return snap

    def compute(self, candles, symbol: Symbol, timeframe: Timeframe, as_of: datetime,
                regime: RegimeSnapshot | None, evaluated_at: datetime | None = None) -> SetupSnapshot:
        as_of = timeframe.floor(as_of)
        series = self.policy.prepare(candles, timeframe, as_of, symbol)
        return self.engine.evaluate(series, lambda t: self._structure_at(series, t),
                                    lambda t: self.features.evaluate(series.upto(t), self.atr_spec),
                                    regime, evaluated_at or datetime.now(timezone.utc))

    def analyze(self, symbol: Symbol, timeframe: Timeframe, as_of: datetime, regime: RegimeSnapshot | None, *,
                save: bool = False) -> SetupSnapshot:
        as_of = timeframe.floor(as_of)
        candles = self.market.last_candles(symbol, timeframe, as_of, self.bars_to_load())  # type: ignore[union-attr]
        snap = self.compute(candles, symbol, timeframe, as_of, regime)
        if save and self.store is not None:
            self.store.save(snap)
        return snap
