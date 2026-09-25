"""Point-in-time historical replay of the EXISTING analytics.

At each bar close t the SetupService is given exactly the candles a live
evaluation at t would load (the last bars_to_load closed bars <= t) and, if
enabled, the RegimeService computes the regime at t from stored data <= t.
No alternative implementation: the replay calls the same services the monitor
calls. The structure cache is keyed by input fingerprints, so it cannot carry
information across time."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence

from app.domain.enums import QualityStatus
from app.domain.market import Candle, Symbol, Timeframe
from app.domain.research_lab import ResearchDataset, canonical
from app.domain.setup import Setup, SetupSnapshot
from app.research.regime.service import RegimeService
from app.research.setup.service import SetupService

_EVAL = datetime(1970, 1, 1, tzinfo=timezone.utc)   # fixed evaluated_at inside replay (not part of any result)


@dataclass
class ReplayObservation:
    setup: Setup                    # as last seen: lifecycle known by last_seen
    first_seen: datetime            # as_of at which the setup first appeared
    last_seen: datetime
    first_regime_fit: str           # regime fit known when it first appeared

    @classmethod
    def from_dict(cls, d: dict) -> "ReplayObservation":
        return cls(Setup.from_dict(d["setup"]), datetime.fromisoformat(d["first_seen"]),
                   datetime.fromisoformat(d["last_seen"]), d["first_regime_fit"])

    def to_dict(self) -> dict:
        return {"setup": self.setup.to_dict(), "first_seen": self.first_seen.isoformat(),
                "last_seen": self.last_seen.isoformat(), "first_regime_fit": self.first_regime_fit}


@dataclass
class ReplayResult:
    run_id: str
    dataset_id: str
    with_regime: bool
    observations: list[ReplayObservation]
    stats: dict = field(default_factory=dict)
    replay_hash: str = ""           # hash chain of every snapshot: identical replay <=> identical hash

    def summary(self) -> dict:
        by_type: dict[str, int] = {}
        for o in self.observations:
            by_type[o.setup.setup_type.value] = by_type.get(o.setup.setup_type.value, 0) + 1
        return {"run_id": self.run_id, "dataset_id": self.dataset_id, "with_regime": self.with_regime,
                "observations": len(self.observations), "by_type": dict(sorted(by_type.items())),
                "replay_hash": self.replay_hash, **self.stats}


class ReplayEngine:
    def __init__(self, setups: SetupService, regime: RegimeService | None = None) -> None:
        self.setups = setups
        self.regime = regime

    def run_id(self, ds: ResearchDataset) -> str:
        return "rp_" + hashlib.sha256(canonical({"dataset": ds.dataset_id, "regime": self.regime is not None,
                                                 "setup_config": self.setups.config.config_hash,
                                                 "regime_config": self.regime.config.config_hash if self.regime
                                                 else None}).encode()).hexdigest()[:20]

    def snapshot_at(self, candles: Sequence[Candle], i: int, symbol: Symbol, tf: Timeframe) -> SetupSnapshot:
        """What the live system would have produced at the close of bar i."""
        load = self.setups.bars_to_load()
        t = candles[i].close_time
        window = candles[max(0, i + 1 - load): i + 1]
        regime = self.regime.analyze(symbol, t, tf) if self.regime is not None else None
        return self.setups.compute(window, symbol, tf, t, regime, _EVAL)

    def run(self, ds: ResearchDataset, candles_by_symbol: dict[str, list[Candle]]) -> ReplayResult:
        tf = Timeframe(ds.spec["timeframe"])
        chain = hashlib.sha256()
        obs: dict[str, ReplayObservation] = {}
        stats = {"evaluations": 0, "valid": 0, "not_valid": {}}
        for sym in ds.spec["symbols"]:
            candles = candles_by_symbol.get(sym, [])
            symbol = Symbol(sym)
            for i in range(len(candles)):
                snap = self.snapshot_at(candles, i, symbol, tf)
                stats["evaluations"] += 1
                chain.update(hashlib.sha256(snap.canonical_json().encode()).digest())
                if snap.data_quality is not QualityStatus.VALID:
                    k = snap.data_quality.value
                    stats["not_valid"][k] = stats["not_valid"].get(k, 0) + 1
                    continue
                stats["valid"] += 1
                for s in snap.setups:
                    prev = obs.get(s.setup_id)
                    if prev is None:
                        obs[s.setup_id] = ReplayObservation(s, snap.as_of, snap.as_of, s.regime_fit.value)
                    else:
                        prev.setup, prev.last_seen = s, snap.as_of
        ordered = sorted(obs.values(), key=lambda o: (o.setup.setup_time, o.setup.symbol, o.setup.setup_id))
        stats["not_valid"] = dict(sorted(stats["not_valid"].items()))
        return ReplayResult(self.run_id(ds), ds.dataset_id, self.regime is not None, ordered, stats,
                            chain.hexdigest()[:24])
