"""Risk configuration snapshot (Phase 9). Values come from app.config RiskSettings (the
first consumer of those settings, finding F3) and are frozen into the run config so a
research run is reproducible on any machine."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from decimal import Decimal

from app.config.settings import RiskSettings
from app.domain.errors import DomainError

MODES = ("equity_pct", "fixed_quote")
_HUNDRED = Decimal(100)


@dataclass(frozen=True)
class RiskConfig:
    mode: str                          # equity_pct (default) | fixed_quote (Phase 7 compatibility only)
    risk_per_trade_pct: Decimal        # percent of equity (0.5 = 0.5 %)
    max_open_risk_pct: Decimal
    max_leverage: Decimal
    max_open_positions: int
    daily_loss_limit_pct: Decimal
    kill_switch_drawdown_pct: Decimal
    hard_drawdown_limit_pct: Decimal
    fixed_risk_quote: Decimal | None   # fixed_quote only

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise DomainError(f"risk mode must be one of {MODES}")
        if self.mode == "fixed_quote" and (self.fixed_risk_quote is None or self.fixed_risk_quote <= 0):
            raise DomainError("fixed_quote needs a positive fixed_risk_quote")
        if self.kill_switch_drawdown_pct > self.hard_drawdown_limit_pct:
            raise DomainError("kill switch must trigger at or before the hard drawdown limit")

    @classmethod
    def from_settings(cls, s: RiskSettings, mode: str = "equity_pct",
                      fixed_risk_quote: Decimal | None = None) -> "RiskConfig":
        s.validate()
        return cls(mode, s.risk_per_trade_pct, s.max_open_risk_pct, s.max_leverage, s.max_open_positions,
                   s.daily_loss_limit_pct, s.kill_switch_drawdown_pct, s.hard_drawdown_limit_pct,
                   None if fixed_risk_quote is None else Decimal(str(fixed_risk_quote)))

    def canonical(self) -> dict:
        return {f.name: (None if (v := getattr(self, f.name)) is None else str(v) if isinstance(v, Decimal) else v)
                for f in fields(self)}

    @property
    def config_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.canonical(), sort_keys=True).encode()).hexdigest()[:16]

    def frac(self, pct: Decimal) -> Decimal:
        return pct / _HUNDRED
