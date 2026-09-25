from __future__ import annotations

import argparse
import json
import subprocess
import sys

import app
from app.config.settings import BUILD_EXECUTION_CEILING, PROJECT_ROOT, load_settings
from app.core.errors import ConfigError, QREError, SafetyViolation
from app.domain.errors import DomainError

EXIT_CONFIG_ERROR = 2      # configuration / safety / invalid input
EXIT_TRANSIENT_ERROR = 5   # temporary failure
from app.core.registry import COMPONENTS
from app.domain.enums import Category
from app.execution.safety import OrderPermission


def _cmd_version(_: argparse.Namespace) -> int:
    print(f"qre {app.__version__} (target {app.TARGET_VERSION}, phase {app.CURRENT_PHASE})")
    return 0


def _cmd_config(_: argparse.Namespace) -> int:
    print(json.dumps(load_settings().redacted(), indent=2, ensure_ascii=False))
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    s = load_settings()
    linear = OrderPermission.check_exchange_submission(s, Category.LINEAR)
    spot = OrderPermission.check_exchange_submission(s, Category.SPOT)
    status = {
        "version": app.__version__,
        "phase": app.CURRENT_PHASE,
        "execution_mode": s.execution_mode.value,
        "build_execution_ceiling": BUILD_EXECUTION_CEILING.value,
        "exchange_orders_allowed": {"linear": linear.allowed, "spot": spot.allowed},
        "block_reason": linear.reason,
        "bybit_env": s.bybit_env.value,
        "api_keys_configured": s.bybit_api_key.is_set(),
        "db_path": str(s.db_path),
        "db_exists": s.db_path.exists(),
        "components": {c.name: ("implemented" if c.implementation else f"planned (phase {c.phase})")
                       for c in COMPONENTS},
    }
    if args.json:
        print(json.dumps(status, indent=2))
    else:
        for k, v in status.items():
            print(f"{k:26} {v}")
    return 0


def _cmd_test(args: argparse.Namespace) -> int:
    cmd = [sys.executable, str(PROJECT_ROOT / "test.py")]
    if args.json:
        cmd.append("--json")
    return subprocess.call(cmd, cwd=PROJECT_ROOT)


def _cmd_db_init(_: argparse.Namespace) -> int:
    from app.storage.database import bootstrap

    s = load_settings()
    conn = bootstrap(s.db_path)
    n = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    print(f"database ready: {s.db_path} ({n} migrations)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app", description="QRE command line")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("version", help="show version").set_defaults(fn=_cmd_version)
    sub.add_parser("config", help="show effective config (secrets redacted)").set_defaults(fn=_cmd_config)
    st = sub.add_parser("status", help="system & safety status")
    st.add_argument("--json", action="store_true")
    st.set_defaults(fn=_cmd_status)
    t = sub.add_parser("test", help="run test.py")
    t.add_argument("--json", action="store_true")
    t.set_defaults(fn=_cmd_test)
    sub.add_parser("db-init", help="create/migrate the local database").set_defaults(fn=_cmd_db_init)
    from app.cli import data_cmds, feature_cmds
    data_cmds.register(sub)
    from app.cli import regime_cmds
    regime_cmds.register(sub)
    from app.cli import monitor_cmds
    monitor_cmds.register(sub)
    from app.cli import structure_cmds
    structure_cmds.register(sub)
    from app.cli import setup_cmds
    setup_cmds.register(sub)
    from app.cli import research_cmds
    research_cmds.register(sub)
    from app.cli import analytics_cmds
    analytics_cmds.register(sub)
    feature_cmds.register(sub)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (ConfigError, SafetyViolation, DomainError, LookupError, ValueError) as e:
        # permanent: repeating cannot fix it (the supervisor does NOT restart on 2)
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except QREError as e:
        # transient (storage busy, market data unavailable ...): the supervisor may restart
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_TRANSIENT_ERROR
