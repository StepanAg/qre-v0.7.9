"""Structure rules versioning: a rules change without bumping STRUCTURE_VERSION is
detected by comparing rules_hash() with VERSION.lock (scripts/lock_structure_version.py)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

_HERE = Path(__file__).resolve().parent
LOCK_FILE = _HERE / "VERSION.lock"
_RULE_SOURCES = ("engine.py", "config.py")


def rules_hash() -> str:
    h = hashlib.sha256()
    for name in _RULE_SOURCES:
        h.update(name.encode())
        h.update((_HERE / name).read_bytes())
    return h.hexdigest()[:16]


def locked() -> dict:
    return json.loads(LOCK_FILE.read_text(encoding="utf-8"))
