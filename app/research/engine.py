"""FeatureEngine: validated series -> per-feature assessment -> formula on the
exact window -> finiteness check -> Feature. Deterministic and point-in-time."""
from __future__ import annotations

import hashlib
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Mapping, Sequence

from app.domain.enums import FeatureStatus, GapSeverity, QualityStatus
from app.domain.market import Candle, Symbol, Timeframe
from app.domain.research import Feature, FeatureSnapshot
from app.research.features.catalog import DEFAULT_REGISTRY
from app.research.features.methodology import NotAvailable
from app.research.features.spec import FeatureRegistry, FeatureSpec, Window
from app.research.quality import DataQualityPolicy, SeriesStats, ValidatedSeries

ENGINE_VERSION = "1"


def feature_set_hash(items: Iterable[tuple[Timeframe, FeatureSpec]]) -> str:
    parts = sorted(f"{tf.value}:{s.key}:{s.formula_hash}" for tf, s in items)
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def input_fingerprint(series: Iterable[tuple[ValidatedSeries, int]]) -> str:
    """Hash of the exact bars (times + exact Decimal values) a snapshot depends on."""
    h = hashlib.sha256()
    for s, n in series:
        h.update(f"{s.symbol}|{s.timeframe.value}|{s.as_of.isoformat()}|{n}".encode())
        for c in s.candles[-n:] if n else []:
            h.update(f"{c.open_time.isoformat()},{c.open},{c.high},{c.low},{c.close},"
                     f"{c.volume},{c.turnover};".encode())
    return h.hexdigest()[:24]


class FeatureEngine:
    def __init__(self, registry: FeatureRegistry = DEFAULT_REGISTRY,
                 policy: DataQualityPolicy = DataQualityPolicy()) -> None:
        self.registry = registry
        self.policy = policy

    # ------------------------------------------------------------ single
    def evaluate(self, series: ValidatedSeries, spec: FeatureSpec) -> Feature:
        base = dict(name=spec.name, timeframe=series.timeframe, as_of=series.as_of, version=spec.version,
                    lookback=spec.lookback, min_observations=spec.min_observations, unit=spec.unit)
        if not spec.implemented:
            return Feature(value=None, status=FeatureStatus.NOT_AVAILABLE, reason=spec.not_implemented_reason,
                           quality=QualityStatus.VALID, **base)
        a = self.policy.assess(series, spec.min_observations, spec.max_gap_bars)
        if not a.ok:
            return Feature(value=None, status=a.status, reason=a.reason, quality=a.quality,
                           gap_severity=a.severity, bars_used=a.tail_bars, **base)
        n = spec.min_observations
        w = Window(series.timeframe, series.o[-n:], series.h[-n:], series.l[-n:], series.c[-n:],
                   series.v[-n:], series.q[-n:])
        try:
            out = spec.fn(w, **spec.params)  # type: ignore[misc]
        except (ZeroDivisionError, ValueError, OverflowError) as e:
            return Feature(value=None, status=FeatureStatus.INVALID, reason=f"calculation error: {e}",
                           quality=QualityStatus.INVALID, gap_severity=a.severity, bars_used=n, **base)
        if isinstance(out, NotAvailable):
            return Feature(value=None, status=FeatureStatus.NOT_AVAILABLE, reason=out.reason,
                           gap_severity=a.severity, bars_used=n, **base)
        if isinstance(out, bool) or not isinstance(out, (int, float)):
            raise TypeError(f"{spec.key} returned {type(out).__name__}; formulas must return float")
        if not math.isfinite(out):
            return Feature(value=None, status=FeatureStatus.INVALID, reason=f"non-finite result {out!r}",
                           quality=QualityStatus.INVALID, gap_severity=a.severity, bars_used=n, **base)
        return Feature(value=float(out), status=FeatureStatus.VALUE, gap_severity=a.severity,
                       bars_used=n, **base)

    def _specs(self, names: Sequence[str] | None) -> list[FeatureSpec]:
        return [self.registry.get(n) for n in names] if names else self.registry.latest()

    # --------------------------------------------------------- snapshots
    def snapshot(self, candles: Sequence[Candle], timeframe: Timeframe, as_of: datetime,
                 names: Sequence[str] | None = None, *, symbol: Symbol | None = None) -> FeatureSnapshot:
        return self.snapshot_mtf({timeframe: candles}, timeframe, as_of, {timeframe: names}, symbol=symbol)

    def snapshot_mtf(self, candles_by_tf: Mapping[Timeframe, Sequence[Candle]], base: Timeframe,
                     as_of: datetime, names_by_tf: Mapping[Timeframe, Sequence[str] | None],
                     *, symbol: Symbol | None = None) -> FeatureSnapshot:
        """Every timeframe is prepared at the SAME as_of, so a higher-timeframe bar
        that has not closed at as_of is simply not part of its series."""
        if base not in names_by_tf:
            raise ValueError("base timeframe must be among requested timeframes")
        features: dict[str, Feature] = {}
        used: list[tuple[Timeframe, FeatureSpec]] = []
        fp: list[tuple[ValidatedSeries, int]] = []
        sym = symbol
        for tf, names in names_by_tf.items():
            series = self.policy.prepare(candles_by_tf.get(tf, ()), tf, as_of, symbol)
            sym = sym or series.symbol
            specs = self._specs(names)
            for spec in specs:
                f = self.evaluate(series, spec)
                features[spec.name if tf is base else f"{tf.value}:{spec.name}"] = f
                used.append((tf, spec))
            fp.append((series, max((s.min_observations for s in specs if s.implemented), default=0)))
        if sym is None:
            raise ValueError("symbol unknown: pass symbol= when no candles are given")
        return FeatureSnapshot(sym, as_of, features, base, feature_set_hash(used), input_fingerprint(fp),
                               ENGINE_VERSION)

    # ---------------------------------------------------------- coverage
    def coverage(self, candles: Sequence[Candle], timeframe: Timeframe, names: Sequence[str],
                 points: Sequence[datetime] | None = None) -> "CoverageReport":
        """Evaluate each feature at every decision point (default: every bar close
        in the data). Uses point-in-time views, so it is also a look-ahead-free
        historical feature calculator."""
        if not candles:
            raise ValueError("no candles")
        last_close = max(c.close_time for c in candles)
        full = self.policy.prepare(candles, timeframe, last_close)
        pts = list(points) if points is not None else [t + timeframe.delta for t in full.times]
        specs = self._specs(names)
        per: dict[str, Counter] = {s.name: Counter() for s in specs}
        for t in pts:
            view = full.upto(t)
            for s in specs:
                f = self.evaluate(view, s)
                per[s.name][_bucket(f)] += 1
        return CoverageReport(timeframe, len(pts), full.stats, per)


def _bucket(f: Feature) -> str:
    if f.status is FeatureStatus.VALUE:
        return "value"
    if f.status is FeatureStatus.DATA_QUALITY_FAILURE:
        return f"data_quality_failure:{f.quality.value}"
    return f.status.value


@dataclass
class CoverageReport:
    timeframe: Timeframe
    points: int
    series_stats: SeriesStats
    per_feature: dict[str, Counter] = field(default_factory=dict)

    def coverage_pct(self, name: str) -> float:
        return 100.0 * self.per_feature[name]["value"] / self.points if self.points else 0.0

    def explain(self, name: str) -> str:
        c = self.per_feature[name]
        excl = ", ".join(f"{100.0 * n / self.points:.1f}% {k}" for k, n in sorted(c.items()) if k != "value")
        return f"{name}: coverage {self.coverage_pct(name):.1f}%" + (f" (excluded: {excl})" if excl else "")

    def totals(self) -> dict[str, int]:
        tot: Counter = Counter()
        for c in self.per_feature.values():
            tot.update(c)
        return dict(tot)
