#!/usr/bin/env python3
"""Refresh app/strategy/VERSION.lock after a DELIBERATE rules change (bump STRATEGY_VERSION first)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.strategy.engine import STRATEGY_VERSION   # noqa: E402
from app.strategy.version import LOCK_FILE, rules_hash  # noqa: E402

LOCK_FILE.write_text(json.dumps({"strategy_version": STRATEGY_VERSION, "rules_hash": rules_hash()}, indent=2) + "\n")
print(LOCK_FILE.read_text())
