"""Filters (TZ section 6). A filter that does not apply to a source raises ValueError
(CLI exit code 2) instead of being silently ignored."""
from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from typing import Sequence

from app.domain.research_lab import SimulatedTrade

DIRECTIONS = ("bullish", "bearish")
REGIME_FITS = ("allowed", "not_allowed", "unknown")


@dataclass(frozen=True)
class AnalyticsFilter:
    start: datetime | None = None          # --from (inclusive), on decision / setup time
    end: datetime | None = None            # --to (exclusive)
    symbol: str | None = None
    direction: str | None = None
    setup: str | None = None
    regime_fit: str | None = None
    regime: str | None = None              # live only (D1 = NO: no regime label for research trades)
    timeframe: str | None = None
    exit_reason: str | None = None

    def __post_init__(self) -> None:
        for t in (self.start, self.end):
            if t is not None and t.tzinfo is None:
                raise ValueError("--from/--to must include a timezone")
        if self.direction and self.direction not in DIRECTIONS:
            raise ValueError(f"--direction must be one of {DIRECTIONS}")
        if self.regime_fit and self.regime_fit not in REGIME_FITS:
            raise ValueError(f"--regime-fit must be one of {REGIME_FITS}")

    def used(self) -> dict:
        return {f.name: (v.isoformat() if isinstance(v, datetime) else v)
                for f in fields(self) if (v := getattr(self, f.name)) is not None}

    def _time_ok(self, t: datetime) -> bool:
        return (self.start is None or t >= self.start) and (self.end is None or t < self.end)

    def trades(self, trades: Sequence[SimulatedTrade], *, timeframe: str,
               regime_fit_of: dict[str, str]) -> list[SimulatedTrade]:
        if self.regime:
            raise ValueError("--regime is not available for research trades (regime label not recorded, "
                             "decision D1); use --regime-fit")
        if self.timeframe and self.timeframe != timeframe:
            return []
        return [t for t in trades
                if self._time_ok(t.decision_time)
                and (self.symbol is None or t.symbol == self.symbol)
                and (self.direction is None or t.direction == self.direction)
                and (self.setup is None or t.setup_type == self.setup)
                and (self.exit_reason is None or t.exit_reason.value == self.exit_reason)
                and (self.regime_fit is None or regime_fit_of.get(t.setup_id, "unknown") == self.regime_fit)]

    def live_rows(self, rows: Sequence[dict], time_key: str) -> list[dict]:
        if self.exit_reason or self.regime_fit:
            raise ValueError("--exit-reason / --regime-fit do not apply to live data")
        return [r for r in rows
                if self._time_ok(r[time_key])
                and (self.symbol is None or r.get("symbol") == self.symbol)
                and (self.timeframe is None or r.get("timeframe") == self.timeframe)
                and (self.direction is None or r.get("direction", self.direction) == self.direction)
                and (self.setup is None or r.get("setup_type", self.setup) == self.setup)
                and (self.regime is None or r.get("regime") == self.regime)]
