"""Single gate every order must pass. Defaults deny."""
from __future__ import annotations

from dataclasses import dataclass

from app.config.settings import BUILD_EXECUTION_CEILING, ExecutionMode, Settings, mode_rank
from app.core.errors import SafetyViolation
from app.domain.enums import Category
from app.domain.execution import Order


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    reason: str


class OrderPermission:
    @staticmethod
    def check_exchange_submission(settings: Settings, category: Category) -> PermissionDecision:
        mode = settings.execution_mode
        if mode_rank(mode) > mode_rank(BUILD_EXECUTION_CEILING):
            return PermissionDecision(False, f"mode {mode.value} above build ceiling")
        if mode in (ExecutionMode.DISABLED, ExecutionMode.PAPER):
            return PermissionDecision(False, f"execution mode is {mode.value}: no exchange orders")
        if mode is ExecutionMode.DEMO and not settings.demo_trading_enabled:
            return PermissionDecision(False, "DEMO_TRADING_ENABLED=false")
        if mode is ExecutionMode.LIVE:
            if not settings.live_trading_enabled:
                return PermissionDecision(False, "LIVE_TRADING_ENABLED=false")
            if category is Category.SPOT and not settings.spot_live_enabled:
                return PermissionDecision(False, "SPOT_LIVE_ENABLED=false")
        return PermissionDecision(True, "ok")

    @classmethod
    def require(cls, settings: Settings, order: Order) -> None:
        d = cls.check_exchange_submission(settings, order.symbol.category)
        if not d.allowed:
            raise SafetyViolation(f"order {order.local_order_id} blocked: {d.reason}")


class DisabledGateway:
    """The only ExecutionGateway in Phase 0. Always refuses."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def submit(self, order: Order) -> Order:
        OrderPermission.require(self._settings, order)
        raise SafetyViolation("no exchange gateway implemented in this build")

    def cancel(self, order: Order) -> Order:
        raise SafetyViolation("no exchange gateway implemented in this build")
