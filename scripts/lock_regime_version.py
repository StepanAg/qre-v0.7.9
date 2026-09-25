#!/usr/bin/env python3
"""Refresh app/research/regime/VERSION.lock after a DELIBERATE rules change.
Bump REGIME_VERSION in rules.py first if the classification semantics changed."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.research.regime.rules import REGIME_VERSION          # noqa: E402
from app.research.regime.version import LOCK_FILE, rules_hash  # noqa: E402

LOCK_FILE.write_text(json.dumps({"regime_version": REGIME_VERSION, "rules_hash": rules_hash()}, indent=2) + "\n")
print(LOCK_FILE.read_text())
