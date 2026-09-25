#!/usr/bin/env python3
"""Regenerate app/research/features/VERSIONS.lock after an INTENTIONAL change.
Rule: if a formula changes, bump its version first; the lock then gains a new
entry and the old entry stays (history of formulas)."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.research.features.catalog import DEFAULT_REGISTRY  # noqa: E402

lock_path = ROOT / "app" / "research" / "features" / "VERSIONS.lock"
lock = json.loads(lock_path.read_text()) if lock_path.exists() else {}
for s in DEFAULT_REGISTRY.all_versions():
    if s.key in lock and lock[s.key] != s.formula_hash and "--force" not in sys.argv:
        sys.exit(f"{s.key}: formula changed without a version bump (use a new version)")
    lock[s.key] = s.formula_hash
lock_path.write_text(json.dumps(dict(sorted(lock.items())), indent=1) + "\n")
print(f"locked {len(lock)} feature versions -> {lock_path.relative_to(ROOT)}")
