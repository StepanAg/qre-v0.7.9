"""`python -m app setup ...` - setup detection (analysis only).
A setup is NOT a trading signal and does not permit opening a position.
Reads stored closed candles only (no network, no orders)."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from app.config.settings import load_settings
from app.domain.market import Symbol, Timeframe
from app.domain.setup import SetupSnapshot
from app.research.setup.config import SetupConfig
from app.research.setup.service import SetupService
from app.research.structure.config import StructureConfig
from app.research.structure.service import StructureService
from app.storage.database import bootstrap
from app.storage.market_data import SQLiteMarketDataStore
from app.storage.setup import SQLiteSetupStore
from app.storage.writer import SerializedWriter

DISCLAIMER = "Setups describe observed situations; they are NOT trading signals and permit no position."


def build(settings, config_path=None):
    w = SerializedWriter(bootstrap(settings.db_path, shared=True))
    market = SQLiteMarketDataStore(w)
    structure = StructureService(market, StructureConfig.from_file(settings.structure_config_file))
    store = SQLiteSetupStore(w)
    svc = SetupService(market, structure, SetupConfig.from_file(config_path or settings.setup_config_file), store)
    return svc, store, market


def _regime(settings, market, symbol: Symbol, tf: Timeframe, as_of: datetime):
    from app.research.regime.config import RegimeConfig
    from app.research.regime.service import RegimeService
    from app.research.service import FeatureService
    rs = RegimeService(FeatureService(market), RegimeConfig.from_file(settings.regime_config_file))
    return rs.analyze(symbol, as_of, tf)


def _line(s) -> str:
    conf = f" confirmed {s.confirmed_at.isoformat()}" if s.confirmed_at else ""
    closed = f" closed {s.closed_at.isoformat()}" if s.closed_at else ""
    return (f"  {s.setup_type.value:16} {s.direction.value:8} {s.status.value:11} level {s.key_level:<10} "
            f"since {s.setup_time.isoformat()}{conf}{closed} regime_fit={s.regime_fit.value}"
            + (f" contra={len(s.contradictions)}" if s.contradictions else ""))


def _summary(snap: SetupSnapshot) -> str:
    lines = [f"{snap.symbol.name} {snap.timeframe.value} @ {snap.as_of.isoformat()}: data {snap.data_quality.value}, "
             f"setups {len(snap.setups)} (active {len(snap.active)})",
             f"regime context: {snap.regime_context['regime'] if snap.regime_context else 'unavailable'}; "
             f"structure: {snap.structure_context['direction'] if snap.structure_context else '-'}"]
    lines += [_line(s) for s in snap.setups] or ["  (no setups)"]
    lines.append("reason codes: " + ", ".join(snap.reason_codes))
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def cmd(a: argparse.Namespace) -> int:
    s = load_settings()
    svc, store, market = build(s, getattr(a, "config", None))
    if a.su_cmd == "status":
        print(json.dumps({"series": store.status(), "snapshots": store.count(), "setups": store.count("setups"),
                          "lifecycle_events": store.count("setup_events"),
                          "conflicts": store.count("setup_conflicts"), "engine_config": svc.config.config_hash},
                         indent=2))
        return 0
    if a.su_cmd == "events":
        evs = store.events(a.setup_id)
        if not evs:
            print(f"no lifecycle events for {a.setup_id}")
            return 1
        for e in evs:
            print(f"{e['at']}  {e['status']:11} {e['reason']}")
        return 0
    sym, tf = Symbol(a.symbol), Timeframe.parse(a.tf)
    if a.su_cmd == "analyze":
        at = datetime.fromisoformat(a.at) if a.at else datetime.now(timezone.utc)
        if at.tzinfo is None:
            raise ValueError("--at must include a timezone, e.g. 2026-03-02T12:00+00:00")
        as_of = tf.floor(at.astimezone(timezone.utc))
        regime = None if a.no_regime else _regime(s, market, sym, tf, as_of)
        snap = svc.analyze(sym, tf, as_of, regime, save=a.save)
        print(json.dumps(snap.to_dict(), indent=2) if a.json else _summary(snap))
        if a.save:
            st = store.last
            print(f"saved: snapshot {'new' if st.snapshot_inserted else 'already stored'}, setups new={st.setups_new} "
                  f"events new={st.events_new} known={st.events_known} conflicts={st.conflicts}")
        return 0
    if a.su_cmd == "list":
        rows = store.setups(sym, tf, a.limit, a.status, a.type)
        for r in rows:
            print(f"{r['setup_time']}  {r['setup_type']:16} {r['direction']:8} {str(r['status']):11} "
                  f"level {r['key_level']}  id {r['setup_id']}")
        if not rows:
            print("no setups recorded")
        return 0
    snap = store.latest(sym, tf)
    if snap is None:
        print(f"{a.symbol} {tf.value}: no setup snapshot stored (run: setup analyze --save)")
        return 1
    if a.su_cmd == "last":
        print(json.dumps(snap.to_dict(), indent=2) if a.json else _summary(snap))
        return 0
    if a.su_cmd == "active":
        print(f"{a.symbol} {tf.value} as of {snap.as_of.isoformat()}: {len(snap.active)} active")
        for x in snap.active:
            print(_line(x))
            print(f"      invalidation: {x.invalidation_condition}")
        print(DISCLAIMER)
        return 0
    return 2


def register(sub) -> None:
    p = sub.add_parser("setup", help="setup detection (analysis only; NOT trading signals)")
    ps = p.add_subparsers(dest="su_cmd", required=True)
    an = ps.add_parser("analyze", help="detect setups at the last closed bar (or --at)")
    an.add_argument("--symbol", required=True)
    an.add_argument("--tf", required=True)
    an.add_argument("--at", help="decision time with timezone (default: now)")
    an.add_argument("--save", action="store_true", help="persist snapshot + lifecycle (idempotent)")
    an.add_argument("--json", action="store_true")
    an.add_argument("--no-regime", action="store_true", help="skip regime context (regime_fit = unknown)")
    an.add_argument("--config", help="setup config JSON (default: SETUP_CONFIG_FILE)")
    for name, hlp in (("last", "latest stored setup snapshot"), ("active", "active setups of the latest snapshot")):
        q = ps.add_parser(name, help=hlp)
        q.add_argument("--symbol", required=True)
        q.add_argument("--tf", required=True)
        if name == "last":
            q.add_argument("--json", action="store_true")
    ls = ps.add_parser("list", help="recorded setups with their latest lifecycle status")
    ls.add_argument("--symbol", required=True)
    ls.add_argument("--tf", required=True)
    ls.add_argument("--status", choices=["candidate", "confirmed", "invalidated", "expired"])
    ls.add_argument("--type", choices=["breakout", "pullback", "sweep_reversal", "range_rejection"])
    ls.add_argument("--limit", type=int, default=30)
    ev = ps.add_parser("events", help="lifecycle history of one setup")
    ev.add_argument("--setup-id", required=True)
    ps.add_parser("status", help="stored setup coverage")
    p.set_defaults(fn=cmd)
