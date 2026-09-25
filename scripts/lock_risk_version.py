#!/usr/bin/env python3
"""Refresh app/risk/VERSION.lock after a DELIBERATE rules change (bump RISK_MODEL_VERSION first)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.risk.engine import RISK_MODEL_VERSION   # noqa: E402
from app.risk.version import LOCK_FILE, rules_hash  # noqa: E402

LOCK_FILE.write_text(json.dumps({"risk_model_version": RISK_MODEL_VERSION, "rules_hash": rules_hash()}, indent=2) + "\n")
print(LOCK_FILE.read_text())
