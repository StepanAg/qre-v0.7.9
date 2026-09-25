"""RegimeService: stored market data -> FeatureService -> RegimeEngine -> store.
Never touches the network; missing/old data shows up as UNKNOWN / STALE."""
from __future__ import annotations

from datetime import datetime

from app.domain.market import Symbol, Timeframe
from app.domain.regime import RegimeSnapshot
from app.domain.research import FeatureSnapshot
from app.research.ports import RegimeSnapshotStore
from app.research.regime.config import ALL_FEATURES, RegimeConfig
from app.research.regime.engine import RegimeEngine
from app.research.service import FeatureService


class RegimeService:
    def __init__(self, features: FeatureService, config: RegimeConfig,
                 store: RegimeSnapshotStore | None = None) -> None:
        self.features = features
        self.config = config
        self.engine = RegimeEngine(config)
        self.store = store

    def bars_per_value(self) -> int:
        """History each timeframe needs (feeds BackfillService.ensure_history)."""
        return self.features.bars_per_value(list(ALL_FEATURES))

    def analysis_timeframes(self, base: Timeframe,
                            timeframes: tuple[Timeframe, ...] | None = None) -> tuple[Timeframe, ...]:
        tfs = timeframes or self.config.timeframes
        if base not in tfs:
            tfs = (base,) + tuple(tfs)
        return tuple(sorted(set(tfs), key=lambda t: t.ms))

    def compute_features(self, symbol: Symbol, as_of: datetime, base: Timeframe,
                         timeframes: tuple[Timeframe, ...] | None = None
                         ) -> tuple[FeatureSnapshot, FeatureSnapshot | None, tuple[Timeframe, ...]]:
        """Step 1 (Feature Engine): point-in-time feature snapshots for the symbol
        (all analysed timeframes) and for BTC context."""
        tfs = self.analysis_timeframes(base, timeframes)
        names = list(ALL_FEATURES)
        snap = self.features.snapshot(symbol, as_of, {tf: names for tf in tfs}, base)
        btc_snap = None
        if symbol != self.config.btc_symbol:
            btc_tf = self.config.btc_timeframe
            # no BTC data / old BTC data -> classified UNKNOWN -> context UNAVAILABLE / STALE
            btc_snap = self.features.snapshot(self.config.btc_symbol, as_of, {btc_tf: names}, btc_tf)
        return snap, btc_snap, tfs

    def classify(self, symbol: Symbol, base: Timeframe, snap: FeatureSnapshot, btc_snap: FeatureSnapshot | None,
                 tfs: tuple[Timeframe, ...]) -> RegimeSnapshot:
        """Step 2 (Regime Engine): pure classification."""
        return self.engine.classify(symbol, snap, base, tfs, btc_snap)

    def analyze(self, symbol: Symbol, as_of: datetime, base: Timeframe,
                timeframes: tuple[Timeframe, ...] | None = None, *, save: bool = False) -> RegimeSnapshot:
        snap, btc_snap, tfs = self.compute_features(symbol, as_of, base, timeframes)
        result = self.classify(symbol, base, snap, btc_snap, tfs)
        if save and self.store is not None:
            self.store.save(result)
        return result
