"""Typed, validated, immutable settings.

Precedence (highest first): explicit `environ` mapping > process env > .env file > defaults.
Tests pass an explicit mapping and `env_file=None`, so they never depend on the
developer's real environment or real keys.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from app.config.env import parse_env_file
from app.core.errors import ConfigError, SafetyViolation
from app.core.logging import register_secret
from app.domain.errors import DomainError
from app.domain.market import Timeframe

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class BybitEnv(str, Enum):
    TESTNET = "testnet"
    DEMO = "demo"
    MAINNET = "mainnet"


class ExecutionMode(str, Enum):
    DISABLED = "disabled"   # no order path at all
    PAPER = "paper"         # internal simulator, never touches the exchange
    DEMO = "demo"           # Bybit demo trading (future)
    LIVE = "live"           # real money (future, requires explicit unlock)


# Hard ceiling compiled into this build. Phase 0 cannot go beyond PAPER no matter
# what the environment says. Raised deliberately in later phases, in code review.
BUILD_EXECUTION_CEILING = ExecutionMode.PAPER
_MODE_ORDER = [ExecutionMode.DISABLED, ExecutionMode.PAPER, ExecutionMode.DEMO, ExecutionMode.LIVE]

_DEFAULT_ENDPOINTS = {
    BybitEnv.TESTNET: ("https://api-testnet.bybit.com", "wss://stream-testnet.bybit.com"),
    BybitEnv.DEMO: ("https://api-demo.bybit.com", "wss://stream.bybit.com"),
    BybitEnv.MAINNET: ("https://api.bybit.com", "wss://stream.bybit.com"),
}


def mode_rank(mode: ExecutionMode) -> int:
    return _MODE_ORDER.index(mode)


class SecretStr:
    """Wrapper that never reveals its value via str/repr/format."""

    __slots__ = ("_value",)

    def __init__(self, value: str | None) -> None:
        self._value = value or ""
        register_secret(self._value)

    def get_secret_value(self) -> str:
        return self._value

    def is_set(self) -> bool:
        return bool(self._value) and not self._value.startswith("your_")

    def __repr__(self) -> str:
        return "SecretStr('***')" if self._value else "SecretStr('')"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SecretStr) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)


@dataclass(frozen=True)
class RiskSettings:
    risk_per_trade_pct: Decimal = Decimal("0.5")
    max_open_risk_pct: Decimal = Decimal("2.5")
    max_leverage: Decimal = Decimal("3")
    max_open_positions: int = 3
    daily_loss_limit_pct: Decimal = Decimal("2")
    kill_switch_drawdown_pct: Decimal = Decimal("15")
    hard_drawdown_limit_pct: Decimal = Decimal("20")

    def validate(self) -> None:
        if not (Decimal("0") < self.risk_per_trade_pct <= Decimal("2")):
            raise ConfigError("RISK_PER_TRADE_PCT must be in (0, 2]")
        if self.max_open_risk_pct < self.risk_per_trade_pct:
            raise ConfigError("MAX_OPEN_RISK_PCT must be >= RISK_PER_TRADE_PCT")
        if not (Decimal("1") <= self.max_leverage <= Decimal("10")):
            raise ConfigError("MAX_LEVERAGE must be in [1, 10]")
        if self.max_open_positions < 1:
            raise ConfigError("MAX_OPEN_POSITIONS must be >= 1")
        if self.kill_switch_drawdown_pct >= self.hard_drawdown_limit_pct:
            raise ConfigError("KILL_SWITCH_DRAWDOWN_PCT must be < HARD_DRAWDOWN_LIMIT_PCT")


@dataclass(frozen=True)
class Settings:
    app_env: str = "dev"
    log_level: str = "INFO"
    db_path: Path = PROJECT_ROOT / "var" / "qre.sqlite3"

    bybit_env: BybitEnv = BybitEnv.TESTNET
    bybit_rest_url: str = _DEFAULT_ENDPOINTS[BybitEnv.TESTNET][0]
    bybit_ws_url: str = _DEFAULT_ENDPOINTS[BybitEnv.TESTNET][1]
    bybit_api_key: SecretStr = field(default_factory=lambda: SecretStr(""))
    bybit_api_secret: SecretStr = field(default_factory=lambda: SecretStr(""))

    # Market data always comes from the MAINNET public API by default: testnet
    # prices/volumes are synthetic and must never feed research or backtests.
    market_data_rest_url: str = "https://api.bybit.com"
    http_timeout_s: Decimal = Decimal("10")
    http_max_attempts: int = 4
    http_min_interval_ms: int = 100
    kline_page_limit: int = 1000

    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")
    timeframes: tuple[Timeframe, ...] = (Timeframe.M15, Timeframe.H1)
    strategy_params_file: Path | None = None

    execution_mode: ExecutionMode = ExecutionMode.DISABLED
    live_trading_enabled: bool = False
    spot_live_enabled: bool = False
    demo_trading_enabled: bool = False

    regime_config_file: Path = PROJECT_ROOT / "config" / "regime_v1.json"
    monitor_config_file: Path = PROJECT_ROOT / "config" / "monitor_v1.json"
    structure_config_file: Path = PROJECT_ROOT / "config" / "structure_v1.json"
    setup_config_file: Path = PROJECT_ROOT / "config" / "setup_v1.json"
    research_config_file: Path = PROJECT_ROOT / "config" / "research_v1.json"
    backtest_config_file: Path = PROJECT_ROOT / "config" / "backtest_v1.json"
    strategy_config_file: Path = PROJECT_ROOT / "config" / "strategy_v1.json"
    decision_run_config_file: Path = PROJECT_ROOT / "config" / "decision_run_v1.json"

    ai_provider: str = "none"
    ai_model: str = ""
    ai_api_key: SecretStr = field(default_factory=lambda: SecretStr(""))

    risk: RiskSettings = field(default_factory=RiskSettings)

    def redacted(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, SecretStr):
                out[f.name] = "***set***" if v.is_set() else "<empty>"
            elif isinstance(v, RiskSettings):
                out[f.name] = {k.name: str(getattr(v, k.name)) for k in fields(v)}
            elif isinstance(v, Enum):
                out[f.name] = v.value
            elif isinstance(v, (Path, Decimal)):
                out[f.name] = str(v)
            elif isinstance(v, tuple):
                out[f.name] = [x.value if isinstance(x, Enum) else x for x in v]
            else:
                out[f.name] = v
        return out


# ------------------------------------------------------------------ parsing
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}
_AI_PROVIDERS = {"none", "gemini", "claude", "openai", "ollama"}


def _bool(name: str, raw: str) -> bool:
    v = raw.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise ConfigError(f"{name}: expected boolean, got {raw!r}")


def _dec(name: str, raw: str) -> Decimal:
    try:
        return Decimal(raw.strip())
    except InvalidOperation as e:
        raise ConfigError(f"{name}: expected decimal, got {raw!r}") from e


def _list(raw: str) -> tuple[str, ...]:
    return tuple(x.strip().upper() for x in raw.split(",") if x.strip())


def _path(raw: str) -> Path:
    p = Path(raw)
    return p if p.is_absolute() else PROJECT_ROOT / p


def load_settings(
    environ: Mapping[str, str] | None = None,
    env_file: Path | None = PROJECT_ROOT / ".env",
    use_process_env: bool | None = None,
) -> Settings:
    if use_process_env is None:
        use_process_env = environ is None
    merged: dict[str, str] = {}
    if env_file is not None:
        merged.update(parse_env_file(Path(env_file)))
    if use_process_env:
        merged.update({k: v for k, v in os.environ.items() if k.isupper()})
    if environ:
        merged.update(environ)

    g = merged.get
    try:
        bybit_env = BybitEnv(g("BYBIT_ENV", "testnet").strip().lower())
    except ValueError as e:
        raise ConfigError(f"BYBIT_ENV invalid: {g('BYBIT_ENV')!r}") from e
    try:
        mode = ExecutionMode(g("EXECUTION_MODE", "disabled").strip().lower())
    except ValueError as e:
        raise ConfigError(f"EXECUTION_MODE invalid: {g('EXECUTION_MODE')!r}") from e

    rest_default, ws_default = _DEFAULT_ENDPOINTS[bybit_env]
    try:
        timeframes = tuple(Timeframe.parse(t) for t in g("TIMEFRAMES", "15m,1h").split(",") if t.strip())
    except DomainError as e:
        raise ConfigError(f"TIMEFRAMES: {e}") from e
    if not timeframes:
        raise ConfigError("TIMEFRAMES must not be empty")

    def _int(name: str, default: str, lo: int, hi: int) -> int:
        try:
            v = int(g(name, default))
        except ValueError as e:
            raise ConfigError(f"{name}: expected integer") from e
        if not lo <= v <= hi:
            raise ConfigError(f"{name} must be in [{lo}, {hi}]")
        return v

    http_timeout = _dec("HTTP_TIMEOUT_S", g("HTTP_TIMEOUT_S", "10"))
    if not Decimal("1") <= http_timeout <= Decimal("60"):
        raise ConfigError("HTTP_TIMEOUT_S must be in [1, 60]")
    md_url = (g("MARKET_DATA_REST_URL", "").strip() or "https://api.bybit.com").rstrip("/")
    if not md_url.startswith("https://"):
        raise ConfigError("MARKET_DATA_REST_URL must use https")
    symbols = _list(g("SYMBOLS", "BTCUSDT,ETHUSDT"))
    if not symbols:
        raise ConfigError("SYMBOLS must not be empty")

    ai_provider = g("AI_PROVIDER", "none").strip().lower()
    if ai_provider not in _AI_PROVIDERS:
        raise ConfigError(f"AI_PROVIDER must be one of {sorted(_AI_PROVIDERS)}")

    db_raw = g("DB_PATH", "").strip()
    db_path = Path(db_raw) if db_raw else PROJECT_ROOT / "var" / "qre.sqlite3"
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path
    spf = g("STRATEGY_PARAMS_FILE", "").strip()

    risk = RiskSettings(
        risk_per_trade_pct=_dec("RISK_PER_TRADE_PCT", g("RISK_PER_TRADE_PCT", "0.5")),
        max_open_risk_pct=_dec("MAX_OPEN_RISK_PCT", g("MAX_OPEN_RISK_PCT", "2.5")),
        max_leverage=_dec("MAX_LEVERAGE", g("MAX_LEVERAGE", "3")),
        max_open_positions=int(g("MAX_OPEN_POSITIONS", "3")),
        daily_loss_limit_pct=_dec("DAILY_LOSS_LIMIT_PCT", g("DAILY_LOSS_LIMIT_PCT", "2")),
        kill_switch_drawdown_pct=_dec("KILL_SWITCH_DRAWDOWN_PCT", g("KILL_SWITCH_DRAWDOWN_PCT", "15")),
        hard_drawdown_limit_pct=_dec("HARD_DRAWDOWN_LIMIT_PCT", g("HARD_DRAWDOWN_LIMIT_PCT", "20")),
    )
    risk.validate()

    settings = Settings(
        app_env=g("APP_ENV", "dev"),
        log_level=g("LOG_LEVEL", "INFO").upper(),
        db_path=db_path,
        bybit_env=bybit_env,
        bybit_rest_url=g("BYBIT_REST_URL", "").strip() or rest_default,
        bybit_ws_url=g("BYBIT_WS_URL", "").strip() or ws_default,
        bybit_api_key=SecretStr(g("BYBIT_API_KEY", "")),
        bybit_api_secret=SecretStr(g("BYBIT_API_SECRET", "")),
        market_data_rest_url=md_url,
        http_timeout_s=http_timeout,
        http_max_attempts=_int("HTTP_MAX_ATTEMPTS", "4", 1, 10),
        http_min_interval_ms=_int("HTTP_MIN_INTERVAL_MS", "100", 0, 10_000),
        kline_page_limit=_int("KLINE_PAGE_LIMIT", "1000", 1, 1000),
        symbols=symbols,
        timeframes=timeframes,
        strategy_params_file=Path(spf) if spf else None,
        execution_mode=mode,
        live_trading_enabled=_bool("LIVE_TRADING_ENABLED", g("LIVE_TRADING_ENABLED", "false")),
        spot_live_enabled=_bool("SPOT_LIVE_ENABLED", g("SPOT_LIVE_ENABLED", "false")),
        demo_trading_enabled=_bool("DEMO_TRADING_ENABLED", g("DEMO_TRADING_ENABLED", "false")),
        regime_config_file=_path(g("REGIME_CONFIG_FILE", "").strip() or "config/regime_v1.json"),
        monitor_config_file=_path(g("MONITOR_CONFIG_FILE", "").strip() or "config/monitor_v1.json"),
        structure_config_file=_path(g("STRUCTURE_CONFIG_FILE", "").strip() or "config/structure_v1.json"),
        setup_config_file=_path(g("SETUP_CONFIG_FILE", "").strip() or "config/setup_v1.json"),
        research_config_file=_path(g("RESEARCH_CONFIG_FILE", "").strip() or "config/research_v1.json"),
        backtest_config_file=_path(g("BACKTEST_CONFIG_FILE", "").strip() or "config/backtest_v1.json"),
        strategy_config_file=_path(g("STRATEGY_CONFIG_FILE", "").strip() or "config/strategy_v1.json"),
        decision_run_config_file=_path(g("DECISION_RUN_CONFIG_FILE", "").strip() or "config/decision_run_v1.json"),
        ai_provider=ai_provider,
        ai_model=g("AI_MODEL", ""),
        ai_api_key=SecretStr(g("AI_API_KEY", "")),
        risk=risk,
    )
    if settings.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ConfigError(f"LOG_LEVEL invalid: {settings.log_level}")
    validate_safety(settings)
    return settings


def validate_safety(s: Settings) -> None:
    """Refuse to start with an unsafe or inconsistent execution configuration."""
    if mode_rank(s.execution_mode) > mode_rank(BUILD_EXECUTION_CEILING):
        raise SafetyViolation(
            f"EXECUTION_MODE={s.execution_mode.value} exceeds this build's ceiling "
            f"({BUILD_EXECUTION_CEILING.value})"
        )
    if s.live_trading_enabled or s.spot_live_enabled:
        raise SafetyViolation("live trading flags cannot be enabled in this build")
    if s.demo_trading_enabled:
        raise SafetyViolation("demo trading cannot be enabled in this build")
    if s.bybit_env is BybitEnv.MAINNET and s.execution_mode is not ExecutionMode.DISABLED \
            and s.execution_mode is not ExecutionMode.PAPER:
        raise SafetyViolation("mainnet requires explicit live unlock")
