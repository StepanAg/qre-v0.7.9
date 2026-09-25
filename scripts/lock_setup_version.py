#!/usr/bin/env python3
"""Refresh app/research/setup/VERSION.lock after a DELIBERATE rules change
(bump SETUP_ENGINE_VERSION in engine.py first if setup semantics changed)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.research.setup.engine import SETUP_ENGINE_VERSION   # noqa: E402
from app.research.setup.version import LOCK_FILE, rules_hash  # noqa: E402

LOCK_FILE.write_text(json.dumps({"engine_version": SETUP_ENGINE_VERSION, "rules_hash": rules_hash()}, indent=2) + "\n")
print(LOCK_FILE.read_text())
