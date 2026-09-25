"""Versions of every analytic component a research result depends on. A change in
any formula / rule / config produces a different dataset and run identity."""
from __future__ import annotations

import hashlib
from pathlib import Path

import app
from app.research.regime.rules import REGIME_VERSION
from app.research.regime.version import rules_hash as regime_rules
from app.research.setup.engine import SETUP_ENGINE_VERSION
from app.research.setup.version import rules_hash as setup_rules
from app.research.structure.engine import STRUCTURE_VERSION
from app.research.structure.version import rules_hash as structure_rules

_FEATURE_LOCK = Path(__file__).resolve().parents[1] / "features" / "VERSIONS.lock"


def feature_catalog_hash() -> str:
    """Covers every feature formula hash (Phase 2 VERSIONS.lock) - closes the audit's
    'features referenced by name' gap for research reproducibility."""
    return hashlib.sha256(_FEATURE_LOCK.read_bytes()).hexdigest()[:16]


def component_versions() -> dict[str, str]:
    return {"code": app.__version__, "feature_catalog": feature_catalog_hash(),
            "regime": f"{REGIME_VERSION}:{regime_rules()}", "structure": f"{STRUCTURE_VERSION}:{structure_rules()}",
            "setup": f"{SETUP_ENGINE_VERSION}:{setup_rules()}"}
