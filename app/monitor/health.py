"""Runtime health: what the monitor is doing and why. Every non-OK outcome has
its own status; nothing is folded into a generic SUCCESS."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class UnitStatus(str, Enum):
    PENDING = "pending"                      # not processed yet in this run
    OK = "ok"                                # regime classified and persisted (or already persisted)
    NO_NEW_BAR = "no_new_bar"                # nothing new since last processing (not logged per tick)
    WAITING_FOR_BAR = "waiting_for_bar"      # expected closed bar not yet delivered by REST (within stale window)
    STALE = "stale"                          # expected bar still missing after stale_after_s
    WARMUP_INSUFFICIENT = "warmup_insufficient"
    DATA_QUALITY_FAILURE = "data_quality_failure"   # gap / duplicate / invalid series
    API_TEMPORARY_ERROR = "api_temporary_error"     # network / 5xx / rate limit after HTTP retries
    API_ERROR = "api_error"                  # non-retryable API error (bad request, unknown symbol)
    INVALID_DATA = "invalid_data"            # malformed response / schema violation
    FEATURE_ERROR = "feature_error"
    REGIME_ERROR = "regime_error"
    PERSISTENCE_ERROR = "persistence_error"
    RETRY_EXHAUSTED = "retry_exhausted"
    INTERNAL_ERROR = "internal_error"


# statuses that count as a failure of the cycle
FAILURE_STATUSES = {UnitStatus.API_TEMPORARY_ERROR, UnitStatus.API_ERROR, UnitStatus.INVALID_DATA,
                    UnitStatus.FEATURE_ERROR, UnitStatus.REGIME_ERROR, UnitStatus.PERSISTENCE_ERROR,
                    UnitStatus.RETRY_EXHAUSTED, UnitStatus.INTERNAL_ERROR}
# statuses after which the unit is retried later for the SAME bar (with backoff)
RETRYABLE_STATUSES = {UnitStatus.API_TEMPORARY_ERROR, UnitStatus.PERSISTENCE_ERROR, UnitStatus.FEATURE_ERROR,
                      UnitStatus.REGIME_ERROR, UnitStatus.INTERNAL_ERROR, UnitStatus.INVALID_DATA}


class StructureStatus(str, Enum):
    """Phase 5 step, tracked separately from the regime outcome of the same unit."""
    OK = "ok"                          # structure snapshot computed on VALID data and persisted
    UNAVAILABLE = "unavailable"        # data quality prevents structure (insufficient history, gap, stale...)
    ERROR = "error"                    # structure engine raised
    PERSISTENCE_ERROR = "persistence_error"
    RETRY_EXHAUSTED = "retry_exhausted"


STRUCTURE_RETRYABLE = {StructureStatus.ERROR, StructureStatus.PERSISTENCE_ERROR}


class SetupStepStatus(str, Enum):
    """Phase 6 step, tracked separately from regime and structure outcomes."""
    OK = "ok"                          # setup snapshot computed on VALID data and persisted
    UNAVAILABLE = "unavailable"        # data quality prevents setup detection
    ERROR = "error"                    # setup engine raised
    PERSISTENCE_ERROR = "persistence_error"
    RETRY_EXHAUSTED = "retry_exhausted"


SETUP_RETRYABLE = {SetupStepStatus.ERROR, SetupStepStatus.PERSISTENCE_ERROR}


class ApiStatus(str, Enum):
    UNKNOWN = "unknown"
    OK = "ok"
    DEGRADED = "degraded"      # last API operation failed
    DOWN = "down"              # api_down_after_failures consecutive failures


class RunStatus(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"        # graceful stop (Ctrl+C / stop request)
    COMPLETED = "completed"    # max runtime reached
    FAILED = "failed"


@dataclass
class UnitState:
    symbol: str
    timeframe: str
    status: UnitStatus = UnitStatus.PENDING
    last_processed_as_of: datetime | None = None   # bar for which processing finished (any terminal outcome)
    last_ok_as_of: datetime | None = None
    last_regime: str | None = None
    last_data_quality: str | None = None
    attempt_bar: datetime | None = None            # bar the current attempt budget belongs to
    attempts_for_bar: int = 0
    consecutive_failures: int = 0
    next_attempt_at: datetime | None = None
    bars_required: int | None = None
    bars_available: int | None = None
    detail: str = ""
    structure_status: StructureStatus | None = None
    structure_last_as_of: datetime | None = None     # bar whose structure step reached a terminal outcome
    structure_attempt_bar: datetime | None = None
    structure_attempts: int = 0
    structure_direction: str | None = None
    structure_detail: str = ""
    setup_status: SetupStepStatus | None = None
    setup_last_as_of: datetime | None = None
    setup_attempt_bar: datetime | None = None
    setup_attempts: int = 0
    setup_active: int | None = None                  # active (candidate/confirmed) setups at the last OK bar
    setup_detail: str = ""

    @property
    def key(self) -> str:
        return f"{self.symbol}/{self.timeframe}"

    def to_dict(self) -> dict:
        iso = lambda t: t.isoformat() if t else None  # noqa: E731
        return {"status": self.status.value, "last_processed_as_of": iso(self.last_processed_as_of),
                "last_ok_as_of": iso(self.last_ok_as_of), "last_regime": self.last_regime,
                "last_data_quality": self.last_data_quality, "attempts_for_bar": self.attempts_for_bar,
                "consecutive_failures": self.consecutive_failures, "next_attempt_at": iso(self.next_attempt_at),
                "bars_required": self.bars_required, "bars_available": self.bars_available,
                "detail": self.detail,
                "structure": {"status": self.structure_status.value if self.structure_status else None,
                              "last_as_of": iso(self.structure_last_as_of), "direction": self.structure_direction,
                              "attempts_for_bar": self.structure_attempts, "detail": self.structure_detail},
                "setup": {"status": self.setup_status.value if self.setup_status else None,
                          "last_as_of": iso(self.setup_last_as_of), "active_setups": self.setup_active,
                          "attempts_for_bar": self.setup_attempts, "detail": self.setup_detail}}


@dataclass
class HealthState:
    run_id: str
    started_at: datetime
    now: datetime
    status: RunStatus = RunStatus.STARTING
    cycles_total: int = 0
    cycles_with_errors: int = 0
    last_cycle_at: datetime | None = None
    last_successful_cycle_at: datetime | None = None
    last_api_success_at: datetime | None = None
    last_api_error_at: datetime | None = None
    last_api_error: str | None = None
    consecutive_api_failures: int = 0
    api_status: ApiStatus = ApiStatus.UNKNOWN
    last_snapshot_saved_at: datetime | None = None
    snapshots_created: int = 0
    snapshots_duplicate: int = 0
    analyses_run: int = 0
    structure_enabled: bool = False
    structure_snapshots_created: int = 0
    structure_events_recorded: int = 0
    structure_failures: int = 0
    setup_enabled: bool = False
    setup_snapshots_created: int = 0
    setups_recorded: int = 0
    setup_failures: int = 0
    outcome_counts: dict[str, int] = field(default_factory=dict)
    units: dict[str, UnitState] = field(default_factory=dict)
    api_requests: int = 0
    api_retries: int = 0

    @property
    def uptime_s(self) -> float:
        return (self.now - self.started_at).total_seconds()

    def count(self, status: UnitStatus) -> None:
        self.count_key(status.value)

    def count_key(self, key: str) -> None:
        self.outcome_counts[key] = self.outcome_counts.get(key, 0) + 1

    def units_with(self, *statuses: UnitStatus) -> list[str]:
        return sorted(k for k, u in self.units.items() if u.status in statuses)

    def to_dict(self) -> dict:
        iso = lambda t: t.isoformat() if t else None  # noqa: E731
        return {
            "run_id": self.run_id, "status": self.status.value, "started_at": iso(self.started_at),
            "now": iso(self.now), "uptime_s": round(self.uptime_s, 1),
            "cycles_total": self.cycles_total, "cycles_with_errors": self.cycles_with_errors,
            "last_cycle_at": iso(self.last_cycle_at), "last_successful_cycle_at": iso(self.last_successful_cycle_at),
            "api_status": self.api_status.value, "last_api_success_at": iso(self.last_api_success_at),
            "last_api_error_at": iso(self.last_api_error_at), "last_api_error": self.last_api_error,
            "consecutive_api_failures": self.consecutive_api_failures,
            "api_requests": self.api_requests, "api_retries": self.api_retries,
            "last_snapshot_saved_at": iso(self.last_snapshot_saved_at),
            "snapshots_created": self.snapshots_created, "snapshots_duplicate": self.snapshots_duplicate,
            "analyses_run": self.analyses_run, "outcome_counts": dict(sorted(self.outcome_counts.items())),
            "structure": {"enabled": self.structure_enabled, "snapshots_created": self.structure_snapshots_created,
                          "events_recorded": self.structure_events_recorded, "failures": self.structure_failures},
            "setup": {"enabled": self.setup_enabled, "snapshots_created": self.setup_snapshots_created,
                      "setups_recorded": self.setups_recorded, "failures": self.setup_failures},
            "stale_units": self.units_with(UnitStatus.STALE, UnitStatus.WAITING_FOR_BAR),
            "unavailable_units": self.units_with(*FAILURE_STATUSES, UnitStatus.WARMUP_INSUFFICIENT,
                                                 UnitStatus.DATA_QUALITY_FAILURE),
            "units": {k: u.to_dict() for k, u in sorted(self.units.items())},
        }
