"""Research Dataset Builder: a reproducible, point-in-time description of which
closed candles a study uses. Candles are REFERENCED (immutable table) and pinned
by fingerprint; nothing is filled, invalid segments are listed with reasons."""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Sequence

from app.data.ports import MarketDataStore
from app.domain.errors import DomainError
from app.domain.market import Candle, Symbol, Timeframe
from app.domain.research_lab import ResearchDataset, Segment, SeriesCoverage, canonical
from app.research.lab.versions import component_versions


class DatasetIntegrityError(DomainError):
    """Stored candles no longer match the dataset fingerprint (data changed)."""


def candle_fingerprint(candles: Sequence[Candle]) -> str:
    h = hashlib.sha256()
    for c in candles:
        h.update(f"{c.open_time.isoformat()}|{c.open}|{c.high}|{c.low}|{c.close}|{c.volume}|{c.turnover};".encode())
    return h.hexdigest()[:24]


def segments_of(candles: Sequence[Candle], tf: Timeframe, min_bars: int) -> tuple[tuple[Segment, ...], int]:
    """Contiguous runs of closed bars. Runs shorter than min_bars (the analytics warm-up)
    are listed as NOT usable with the reason; gaps are never filled."""
    if not candles:
        return (), 0
    runs, cur, missing = [], [candles[0]], 0
    for a, b in zip(candles, candles[1:]):
        if b.open_time - a.open_time == tf.delta:
            cur.append(b)
        else:
            missing += (b.open_time - a.open_time) // tf.delta - 1
            runs.append(cur)
            cur = [b]
    runs.append(cur)
    segs = tuple(Segment(r[0].open_time, r[-1].close_time, len(r), len(r) >= min_bars,
                         "ok" if len(r) >= min_bars else f"shorter_than_warmup:{len(r)}<{min_bars}") for r in runs)
    return segs, missing


class DatasetBuilder:
    def __init__(self, market: MarketDataStore, *, min_segment_bars: int, config_hashes: dict[str, str],
                 context_series: Sequence[tuple[str, Timeframe]] = ()) -> None:
        self.market = market
        self.min_segment_bars = min_segment_bars       # = the setup engine's contiguous-history requirement
        self.config_hashes = dict(config_hashes)
        self.context_series = tuple(context_series)    # regime timeframes + BTC, when regime is replayed

    def _coverage(self, sym: str, tf: Timeframe, start: datetime, end: datetime, role: str
                  ) -> tuple[SeriesCoverage, list[Candle]]:
        candles = self.market.get_candles(Symbol(sym), tf, start, end)
        segs, missing = segments_of(candles, tf, self.min_segment_bars if role == "primary" else 1)
        return SeriesCoverage(sym, tf.value, role, len(candles), candle_fingerprint(candles), segs, missing), candles

    def build(self, symbols: Sequence[str], timeframe: Timeframe, start: datetime, end: datetime, *,
              with_context: bool = False) -> ResearchDataset:
        if not symbols or len(set(symbols)) != len(symbols):
            raise DomainError("symbols must be non-empty and unique")
        start, end = timeframe.ceil(start), timeframe.floor(end)
        if end <= start:
            raise DomainError("empty dataset range")
        spec = {"symbols": list(symbols), "timeframe": timeframe.value, "start": start.isoformat(),
                "end": end.isoformat(), "with_context": with_context}
        covs: list[SeriesCoverage] = []
        for sym in symbols:
            covs.append(self._coverage(sym, timeframe, start, end, "primary")[0])
        if with_context:
            for sym in symbols:
                for s, tf in self.context_series:
                    target = sym if s == "*" else s
                    if (target, tf.value) not in {(c.symbol, c.timeframe) for c in covs}:
                        covs.append(self._coverage(target, tf, start - tf.delta * self.min_segment_bars * 2,
                                                   end, "context")[0])
        funding = {}
        for sym in symbols:
            rates = self.market.funding_rates(Symbol(sym), start, end)
            h = hashlib.sha256("".join(f"{r.funding_time.isoformat()}|{r.rate};" for r in rates).encode())
            funding[sym] = {"count": len(rates), "fingerprint": h.hexdigest()[:24]}
        prim = [c for c in covs if c.role == "primary"]
        summary = {"primary_candles": sum(c.candles for c in prim),
                   "missing_bars": sum(c.missing_bars for c in prim),
                   "usable_segments": sum(1 for c in prim for s in c.segments if s.usable),
                   "excluded_segments": [{"symbol": c.symbol, **s.to_dict()} for c in prim for s in c.segments
                                         if not s.usable],
                   "symbols_without_data": [c.symbol for c in prim if c.candles == 0]}
        versions = component_versions()
        ident = canonical({"spec": spec, "versions": versions, "configs": self.config_hashes,
                           "data": [(c.symbol, c.timeframe, c.role, c.fingerprint) for c in covs],
                           "funding": funding})
        dataset_id = "ds_" + hashlib.sha256(ident.encode()).hexdigest()[:20]
        return ResearchDataset(dataset_id, spec, tuple(covs), funding, versions, self.config_hashes, summary)

    def verify(self, ds: ResearchDataset) -> None:
        """Refuse to use a dataset whose stored inputs changed (no silent drift)."""
        for c in ds.coverages:
            tf = Timeframe(c.timeframe)
            start = datetime.fromisoformat(ds.spec["start"])
            if c.role == "context":
                start = start - tf.delta * self.min_segment_bars * 2
            now = self._coverage(c.symbol, tf, start, datetime.fromisoformat(ds.spec["end"]), c.role)[0]
            if now.fingerprint != c.fingerprint:
                raise DatasetIntegrityError(f"{ds.dataset_id}: candles of {c.symbol} {c.timeframe} changed since the "
                                            "dataset was built; build a new dataset version")
        if component_versions() != dict(ds.versions):
            raise DatasetIntegrityError(f"{ds.dataset_id}: analytic component versions differ from the dataset "
                                        f"({dict(ds.versions)} -> {component_versions()}); build a new dataset")

    def candles(self, ds: ResearchDataset, symbol: str) -> list[Candle]:
        tf = Timeframe(ds.spec["timeframe"])
        return self.market.get_candles(Symbol(symbol), tf, datetime.fromisoformat(ds.spec["start"]),
                                       datetime.fromisoformat(ds.spec["end"]))
