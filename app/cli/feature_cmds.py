"""`python -m app features ...` - catalog and point-in-time snapshots from the local DB."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from app.config.settings import load_settings
from app.domain.market import Symbol, Timeframe
from app.research.features.catalog import DEFAULT_REGISTRY
from app.research.service import FeatureService
from app.storage.database import bootstrap
from app.storage.features import SQLiteFeatureSnapshotStore
from app.storage.market_data import SQLiteMarketDataStore
from app.storage.writer import SerializedWriter


def cmd(a: argparse.Namespace) -> int:
    if a.feat_cmd == "catalog":
        for s in DEFAULT_REGISTRY.latest():
            flag = " [PROXY]" if s.is_proxy else (" [NOT IMPLEMENTED]" if not s.implemented else "")
            print(f"{s.key:34} {s.family:10} unit={s.unit:12} min_obs={s.min_observations:4} "
                  f"warmup={s.warmup:3}{flag}")
        return 0
    s = load_settings()
    w = SerializedWriter(bootstrap(s.db_path, shared=True))
    svc = FeatureService(SQLiteMarketDataStore(w), cache=SQLiteFeatureSnapshotStore(w) if a.save else None)
    at = datetime.fromisoformat(a.at).astimezone(timezone.utc) if a.at else datetime.now(timezone.utc)
    tf = Timeframe.parse(a.tf)
    names_by_tf = {tf: a.names or None}
    for extra in a.mtf or []:
        names_by_tf[Timeframe.parse(extra)] = a.names or None
    snap = svc.snapshot(Symbol(a.symbol), at, names_by_tf, tf)
    rows = {k: {"status": f.status.value, "value": f.value, "unit": f.unit, "version": f.version,
                "quality": f.quality.value, "gap": f.gap_severity.value, "reason": f.reason}
            for k, f in sorted(snap.features.items())}
    print(json.dumps({"symbol": a.symbol, "as_of": at.isoformat(), "feature_set": snap.feature_set_hash,
                      "input": snap.input_fingerprint, "features": rows}, indent=2))
    return 0


def register(sub) -> None:
    f = sub.add_parser("features", help="feature catalog and snapshots (no network)")
    fs = f.add_subparsers(dest="feat_cmd", required=True)
    fs.add_parser("catalog", help="list registered features with requirements")
    s = fs.add_parser("snapshot", help="compute a point-in-time snapshot from stored candles")
    s.add_argument("--symbol", required=True)
    s.add_argument("--tf", required=True)
    s.add_argument("--at", help="decision time with timezone (default: now)")
    s.add_argument("--names", nargs="*")
    s.add_argument("--mtf", nargs="*", help="additional timeframes, e.g. 1h 4h")
    s.add_argument("--save", action="store_true", help="persist/cache the snapshot")
    f.set_defaults(fn=cmd)
