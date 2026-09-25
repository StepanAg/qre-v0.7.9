from __future__ import annotations

from datetime import datetime
from typing import Protocol, Sequence

from app.domain.market import Candle, Symbol, Timeframe
from app.domain.regime import RegimeSnapshot
from app.domain.setup import SetupSnapshot
from app.domain.structure import StructureSnapshot
from app.domain.research import FeatureSnapshot


class FeatureCalculator(Protocol):
    """Pure: candles known at as_of -> FeatureSnapshot. Implemented by FeatureEngine."""

    def snapshot(self, candles: Sequence[Candle], timeframe: Timeframe, as_of: datetime,
                 names: Sequence[str] | None = None, *, symbol: Symbol | None = None) -> FeatureSnapshot: ...


class FeatureSnapshotStore(Protocol):
    """Persistence / cache of snapshots, keyed by
    (symbol, timeframe, as_of, feature_set_hash, input_fingerprint)."""

    def save(self, snapshot: FeatureSnapshot) -> bool: ...
    def load(self, symbol: Symbol, timeframe: Timeframe, as_of: datetime, feature_set_hash: str,
             input_fingerprint: str) -> FeatureSnapshot | None: ...


class RegimeSnapshotStore(Protocol):
    """Phase 3: immutable, idempotent persistence of regime snapshots."""

    def save(self, snapshot: RegimeSnapshot) -> bool: ...
    def load(self, key: tuple) -> RegimeSnapshot | None: ...


class StructureSnapshotStore(Protocol):
    """Phase 5: immutable snapshots + append-only structure/sweep event log."""

    def save(self, snapshot: StructureSnapshot) -> bool: ...


class SetupSnapshotStore(Protocol):
    """Phase 6: immutable setup snapshots + append-only setup lifecycle log."""

    def save(self, snapshot: SetupSnapshot) -> bool: ...


class ResearchStore(Protocol):
    """Phase 7: immutable research datasets and runs (datasets, replay, outcomes, backtests)."""

    def save_dataset(self, manifest: dict) -> bool: ...
    def dataset(self, dataset_id: str) -> dict | None: ...
    def save_run(self, **kw) -> bool: ...
    def run(self, run_id: str) -> dict | None: ...
    def observations(self, run_id: str) -> list[dict]: ...
