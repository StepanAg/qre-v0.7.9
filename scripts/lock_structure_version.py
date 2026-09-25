#!/usr/bin/env python3
"""Refresh app/research/structure/VERSION.lock after a DELIBERATE rules change
(bump STRUCTURE_VERSION in engine.py first if event semantics changed)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.research.structure.engine import STRUCTURE_VERSION     # noqa: E402
from app.research.structure.version import LOCK_FILE, rules_hash  # noqa: E402

LOCK_FILE.write_text(json.dumps({"structure_version": STRUCTURE_VERSION, "rules_hash": rules_hash()}, indent=2) + "\n")
print(LOCK_FILE.read_text())
