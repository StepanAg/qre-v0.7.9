"""`python -m app regime ...` - current market regime from stored data.
Wiring only. No orders; network is used only with --refresh (public market data)."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from app.config.settings import load_settings
from app.domain.market import Symbol, Timeframe
from app.research.regime.config import RegimeConfig
from app.research.regime.service import RegimeService
from app.research.service import FeatureService
from app.storage.database import bootstrap
from app.storage.features import SQLiteFeatureSnapshotStore
from app.storage.market_data import SQLiteMarketDataStore
from app.storage.regime import SQLiteRegimeSnapshotStore
from app.storage.writer import SerializedWriter


def build(settings, config_path=None):
    cfg = RegimeConfig.from_file(config_path or settings.regime_config_file)
    w = SerializedWriter(bootstrap(settings.db_path, shared=True))
    md = SQLiteMarketDataStore(w)
    svc = RegimeService(FeatureService(md, cache=SQLiteFeatureSnapshotStore(w)), cfg, SQLiteRegimeSnapshotStore(w))
    return svc, md


def refresh(settings, svc: RegimeService, symbol: Symbol, tfs, as_of: datetime, transport=None) -> list[str]:
    """Download exactly the history the regime needs (public endpoints only)."""
    from app.cli.data_cmds import build as build_data
    from app.data.backfill import plan_history
    from app.domain.enums import Category
    backfill, _, _ = build_data(settings, transport)
    per_value = svc.bars_per_value()
    names = sorted({symbol.name, svc.config.btc_symbol.name})
    backfill.sync_instruments(Category.LINEAR, names)
    lines = []
    jobs = [(symbol, tf) for tf in tfs]
    if symbol != svc.config.btc_symbol:
        jobs.append((svc.config.btc_symbol, svc.config.btc_timeframe))
    for sym, tf in jobs:
        p = plan_history(tf, 1, per_value, as_of)
        r = backfill.backfill(sym, tf, p.start, p.end)
        lines.append(f"{sym.name} {tf.value}: {r.status} inserted={r.inserted} gaps={len(r.gaps)}")
    return lines


def cmd(a: argparse.Namespace) -> int:
    s = load_settings()
    svc, _ = build(s, a.config)
    cfg = svc.config
    if a.regime_cmd == "history":
        for r in svc.store.history(Symbol(a.symbol), Timeframe.parse(a.tf), a.limit):  # type: ignore[union-attr]
            print(f"{r.as_of.isoformat()} {r.regime.value:15} {r.trend_direction.value:8} "
                  f"{r.volatility_state.value:7} mtf={r.mtf_alignment.value:8} "
                  f"btc={r.btc_context.status.value if r.btc_context else '-'} q={r.data_quality.value}")
        return 0
    base = Timeframe.parse(a.tf) if a.tf else cfg.timeframes[0]
    tfs = tuple(sorted({base, *(Timeframe.parse(t) for t in (a.mtf if a.mtf is not None else
                                                             [t.value for t in cfg.timeframes]))},
                       key=lambda t: t.ms))
    at = datetime.fromisoformat(a.at) if a.at else datetime.now(timezone.utc)
    if at.tzinfo is None:
        raise ValueError("--at must include a timezone, e.g. 2026-03-02T12:00+00:00")
    as_of = base.floor(at.astimezone(timezone.utc))      # last CLOSED base bar
    symbol = Symbol(a.symbol)
    if a.refresh:
        for line in refresh(s, svc, symbol, tfs, as_of):
            print("refresh:", line)
    snap = svc.analyze(symbol, as_of, base, tfs, save=a.save)
    out = snap.to_dict()
    out["summary"] = (f"{snap.symbol.name} {base.value} @ {snap.as_of.isoformat()}: {snap.regime.value} "
                      f"(trend {snap.trend_direction.value}/{snap.trend_strength.value}, "
                      f"vol {snap.volatility_state.value}, mtf {snap.mtf_alignment.value}, "
                      f"btc {snap.btc_context.status.value if snap.btc_context else '-'}, "
                      f"data {snap.data_quality.value})")
    out["unknown_reasons"] = [c for t in snap.timeframes for c in t.reason_codes
                              if c.startswith(("UNKNOWN:", "REQ_FEATURE"))]
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def register(sub) -> None:
    r = sub.add_parser("regime", help="market regime (research only, no orders)")
    rs = r.add_subparsers(dest="regime_cmd", required=True)
    s = rs.add_parser("snapshot", help="classify the regime at the last closed bar")
    s.add_argument("--symbol", required=True)
    s.add_argument("--tf", help="base timeframe (default: first in regime config)")
    s.add_argument("--mtf", nargs="*", help="timeframes to analyse (default: regime config)")
    s.add_argument("--at", help="decision time with timezone (default: now)")
    s.add_argument("--save", action="store_true", help="persist the snapshot (idempotent)")
    s.add_argument("--refresh", action="store_true", help="download needed public history first (network)")
    s.add_argument("--config", help="regime config JSON (default: REGIME_CONFIG_FILE)")
    h = rs.add_parser("history", help="stored regime snapshots")
    h.add_argument("--symbol", required=True)
    h.add_argument("--tf", required=True)
    h.add_argument("--limit", type=int, default=20)
    h.add_argument("--config")
    r.set_defaults(fn=cmd)
