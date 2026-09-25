"""FeatureService: loads exactly the needed history from the MarketDataStore
port, computes a snapshot, and uses the snapshot store as a cache.

Never fetches from the network: missing history is reported as
INSUFFICIENT_HISTORY / STALE. Ingestion (backfill) is a separate step."""
from __future__ import annotations

from datetime import datetime
from typing import Mapping, Sequence

from app.data.ports import MarketDataStore
from app.domain.market import Symbol, Timeframe
from app.domain.research import FeatureSnapshot
from app.research.engine import FeatureEngine, feature_set_hash, input_fingerprint
from app.research.ports import FeatureSnapshotStore

# One extra bar is loaded beyond the requirement so that a missing latest bar
# shows up as STALE rather than being silently replaced by an older bar.
_EXTRA = 1


class FeatureService:
    def __init__(self, store: MarketDataStore, engine: FeatureEngine | None = None,
                 cache: FeatureSnapshotStore | None = None) -> None:
        self.store = store
        self.engine = engine or FeatureEngine()
        self.cache = cache
        self.cache_hits = 0
        self.cache_misses = 0

    def required_bars(self, names: Sequence[str] | None) -> int:
        """Contiguous closed bars ONE snapshot of these features depends on."""
        specs = self.engine._specs(names)
        return max((s.min_observations for s in specs if s.implemented), default=1)

    def bars_per_value(self, names: Sequence[str] | None) -> int:
        """What ingestion must pass to BackfillService.ensure_history/plan_history.
        Includes the extra bar that lets a missing latest bar surface as STALE."""
        return self.required_bars(names) + _EXTRA

    def snapshot(self, symbol: Symbol, as_of: datetime, names_by_tf: Mapping[Timeframe, Sequence[str] | None],
                 base: Timeframe) -> FeatureSnapshot:
        candles = {}
        for tf, names in names_by_tf.items():
            # open_time < floor(as_of) <=> close_time <= floor(as_of) <= as_of
            candles[tf] = self.store.last_candles(symbol, tf, tf.floor(as_of),
                                                  self.required_bars(names) + _EXTRA)
        if self.cache is not None:
            used = [(tf, s) for tf, names in names_by_tf.items() for s in self.engine._specs(names)]
            fs_hash = feature_set_hash(used)
            fp = input_fingerprint(
                (self.engine.policy.prepare(candles[tf], tf, as_of, symbol),
                 max((s.min_observations for s in self.engine._specs(n) if s.implemented), default=0))
                for tf, n in names_by_tf.items())
            hit = self.cache.load(symbol, base, as_of, fs_hash, fp)
            if hit is not None:
                self.cache_hits += 1
                return hit
            self.cache_misses += 1
        snap = self.engine.snapshot_mtf(candles, base, as_of, names_by_tf, symbol=symbol)
        if self.cache is not None:
            self.cache.save(snap)
        return snap
