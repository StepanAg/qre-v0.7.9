"""`python -m app structure ...` - market structure & OHLCV-derived liquidity.
Reads stored closed candles only (no network, no orders)."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from app.config.settings import load_settings
from app.domain.market import Symbol, Timeframe
from app.domain.structure import StructureEvent, StructureSnapshot
from app.research.structure.config import StructureConfig
from app.research.structure.service import StructureService
from app.storage.database import bootstrap
from app.storage.market_data import SQLiteMarketDataStore
from app.storage.structure import SQLiteStructureStore
from app.storage.writer import SerializedWriter


def build(settings, config_path=None) -> tuple[StructureService, SQLiteStructureStore]:
    w = SerializedWriter(bootstrap(settings.db_path, shared=True))
    store = SQLiteStructureStore(w)
    cfg = StructureConfig.from_file(config_path or settings.structure_config_file)
    return StructureService(SQLiteMarketDataStore(w), cfg, store), store


def _summary(s: StructureSnapshot) -> str:
    e = s.events[-1] if s.events else None
    last = f"{e.event_type.value} {e.direction.value} @ {e.event_time.isoformat()} lvl {e.level_price}" if e else "-"
    return (f"{s.symbol.name} {s.timeframe.value} @ {s.as_of.isoformat()}: direction {s.direction.value} "
            f"(since {s.direction_since.isoformat() if s.direction_since else '-'}), data {s.data_quality.value}, "
            f"swings {len(s.swing_highs)}H/{len(s.swing_lows)}L, last event: {last}, "
            f"EQH {len(s.equal_highs)}, EQL {len(s.equal_lows)}, active liquidity {len(s.liquidity)}, "
            f"recent sweeps {len(s.sweeps)}")


def _levels(s: StructureSnapshot) -> None:
    print("OHLCV-derived potential liquidity (hypotheses from price, NOT order-book data):")
    for lv in sorted(s.liquidity, key=lambda x: -x.price):
        zone = f"{lv.zone_low}-{lv.zone_high}" if lv.zone_low != lv.zone_high else f"{lv.price}"
        print(f"  {lv.side.value:9} {lv.source.value:12} {zone:>22}  since {lv.created_at.isoformat()}  "
              f"members={len(lv.source_pivots)}")
    if not s.liquidity:
        print("  (none)")


def cmd(a: argparse.Namespace) -> int:
    s = load_settings()
    svc, store = build(s, getattr(a, "config", None))
    if a.st_cmd == "status":
        rows = store.status()
        print(json.dumps({"series": rows, "snapshots": store.count(), "events": store.count("structure_events"),
                          "classification_conflicts": store.count("structure_event_conflicts")}, indent=2))
        return 0
    sym, tf = Symbol(a.symbol), Timeframe.parse(a.tf)
    if a.st_cmd == "analyze":
        at = datetime.fromisoformat(a.at) if a.at else datetime.now(timezone.utc)
        if at.tzinfo is None:
            raise ValueError("--at must include a timezone, e.g. 2026-03-02T12:00+00:00")
        snap = svc.analyze(sym, tf, at.astimezone(timezone.utc), save=a.save)
        print(json.dumps(snap.to_dict(), indent=2) if a.json else _summary(snap))
        if not a.json:
            print("reason codes:", ", ".join(snap.reason_codes))
        if a.save:
            st = store.last
            print(f"saved: snapshot {'new' if st.snapshot_inserted else 'already stored'}, "
                  f"events new={st.events_inserted} known={st.events_known} conflicts={st.conflicts}")
        return 0
    snap = store.latest(sym, tf)
    if a.st_cmd == "last":
        if snap is None:
            print(f"{a.symbol} {tf.value}: no structure snapshot stored (run: structure analyze --save)")
            return 1
        print(json.dumps(snap.to_dict(), indent=2) if a.json else _summary(snap))
        return 0
    if a.st_cmd == "liquidity":
        if snap is None:
            print(f"{a.symbol} {tf.value}: no structure snapshot stored")
            return 1
        print(f"as of {snap.as_of.isoformat()}")
        _levels(snap)
        return 0
    if a.st_cmd == "events":
        evs = store.events(sym, tf, a.limit, tuple(a.kind) if a.kind else None)
        for e in reversed(evs):
            if isinstance(e, StructureEvent):
                print(f"{e.event_time.isoformat()}  {e.event_type.value:18} {e.direction.value:8} "
                      f"level {e.level_price} (swing {e.swing_pivot_time.isoformat()}) prior={e.prior_direction.value}")
            else:
                print(f"{e.event_time.isoformat()}  {'sweep':18} {e.side.value:8} level {e.level_price} "
                      f"extreme {e.extreme} close {e.close}")
        if not evs:
            print("no events recorded")
        return 0
    return 2


def register(sub) -> None:
    p = sub.add_parser("structure", help="market structure & OHLCV-derived liquidity (analysis only)")
    ps = p.add_subparsers(dest="st_cmd", required=True)
    an = ps.add_parser("analyze", help="compute structure at the last closed bar (or --at)")
    for q in (an,):
        q.add_argument("--symbol", required=True)
        q.add_argument("--tf", required=True)
    an.add_argument("--at", help="decision time with timezone (default: now)")
    an.add_argument("--save", action="store_true", help="persist snapshot + events (idempotent)")
    an.add_argument("--json", action="store_true")
    an.add_argument("--config", help="structure config JSON (default: STRUCTURE_CONFIG_FILE)")
    la = ps.add_parser("last", help="latest stored structure snapshot")
    la.add_argument("--symbol", required=True)
    la.add_argument("--tf", required=True)
    la.add_argument("--json", action="store_true")
    ev = ps.add_parser("events", help="recorded BOS / CHoCH / unclassified breaks / sweeps")
    ev.add_argument("--symbol", required=True)
    ev.add_argument("--tf", required=True)
    ev.add_argument("--limit", type=int, default=30)
    ev.add_argument("--kind", nargs="*", choices=["bos", "choch", "break_unclassified", "sweep"])
    lq = ps.add_parser("liquidity", help="active liquidity levels of the latest snapshot")
    lq.add_argument("--symbol", required=True)
    lq.add_argument("--tf", required=True)
    ps.add_parser("status", help="stored structure coverage")
    p.set_defaults(fn=cmd)
