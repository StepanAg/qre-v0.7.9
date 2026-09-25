"""Data quality, anomalies (TZ 5.14) and monitor / live-structure summaries (TZ 5.13).

Diagnoses only: never corrects, never writes. Every finding = table, record, problem,
severity (critical / warning / info), category (invalid / inconsistent / orphaned /
duplicated / incomplete / integrity). Deterministic: no wall clock is used."""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Sequence

from app.analytics.engine import PNL_TOLERANCE, pnl_consistent
from app.analytics.ports import AnalyticsSource
from app.domain.market import Timeframe
from app.monitor.health import FAILURE_STATUSES
from app.research.lab import stats

CONFLICT_TABLES = ("setup_conflicts", "structure_event_conflicts", "research_conflicts")
TERMINAL = {"invalidated", "expired"}
_ORDER = {"candidate": 0, "confirmed": 1, "invalidated": 2, "expired": 2}


def _f(table, record, problem, severity, category) -> dict:
    return {"table": table, "record": str(record), "problem": problem, "severity": severity, "category": category}


# --------------------------------------------------------------- research
def _check_trades(src: AnalyticsSource, runs: dict, out: list) -> None:
    per_run_setups: dict[tuple, int] = {}
    for row in src.trade_rows():
        p, rid = row["payload"], (row["run_id"], row["trade_no"])
        rec = f"{row['run_id']}#{row['trade_no']}"
        if row["run_id"] not in runs:
            out.append(_f("sim_trades", rec, "orphan: run missing", "critical", "orphaned"))
        elif src.dataset(row["dataset_id"]) is None:
            out.append(_f("sim_trades", rec, "orphan: dataset missing", "critical", "orphaned"))
        T = datetime.fromisoformat
        try:
            dec, ent, ext = T(p["decision_time"]), T(p["entry_time"]), T(p["exit_time"])
        except (KeyError, ValueError, TypeError):
            out.append(_f("sim_trades", rec, "missing or invalid timestamps", "critical", "invalid"))
            continue
        if ext < ent:
            out.append(_f("sim_trades", rec, "exit_time < entry_time", "critical", "invalid"))
        if ent < dec:
            out.append(_f("sim_trades", rec, "entry_time < decision_time", "critical", "invalid"))
        if not isinstance(p.get("bars_held"), int) or p["bars_held"] <= 0:
            out.append(_f("sim_trades", rec, "bars_held <= 0", "critical", "invalid"))
        if not pnl_consistent(p["gross_pnl"], p["fees"], p["funding"], p["net_pnl"]):
            out.append(_f("sim_trades", rec, "net != gross - fees + funding", "critical", "inconsistent"))
        if p["initial_risk"] is None or p["initial_risk"] <= 0:
            out.append(_f("sim_trades", rec, "initial_risk <= 0 (R undefined)", "warning", "invalid"))
        elif p["r_multiple"] is not None and abs(p["r_multiple"] - p["net_pnl"] / p["initial_risk"]) > \
                PNL_TOLERANCE * max(1.0, abs(p["r_multiple"])):
            out.append(_f("sim_trades", rec, "r_multiple != net / initial_risk", "warning", "inconsistent"))
        k = (row["run_id"], p.get("setup_id"))
        per_run_setups[k] = per_run_setups.get(k, 0) + 1
        _ = rid
    for (run_id, setup_id), n in sorted(per_run_setups.items()):
        if n > 1:
            out.append(_f("sim_trades", f"{run_id}/{setup_id}", f"{n} trades for one setup in a run", "warning",
                          "duplicated"))


def _check_trade_windows(src: AnalyticsSource, runs: dict, out: list) -> None:
    for rid, run in runs.items():
        if run["kind"] != "backtest":
            continue
        ds = src.dataset(run["dataset_id"])
        if ds is None:
            continue
        tf = Timeframe(ds["spec"]["timeframe"])
        for row in src.trade_rows(rid):
            p = row["payload"]
            try:
                a, b = datetime.fromisoformat(p["entry_time"]), datetime.fromisoformat(p["exit_time"])
            except (KeyError, ValueError, TypeError):
                continue
            n = len(src.candles(p["symbol"], tf.value, a, b))
            if b > a and n != int((b - a) / tf.delta):
                out.append(_f("sim_trades", f"{rid}#{row['trade_no']}", f"trade window not covered by candles "
                              f"({n} of {int((b - a) / tf.delta)} bars)", "warning", "incomplete"))


def _check_equity(src: AnalyticsSource, runs: dict, out: list) -> None:
    for rid in sorted(src.run_ids_of("sim_equity")):
        if rid not in runs:
            out.append(_f("sim_equity", rid, "orphan: run missing", "critical", "orphaned"))
            continue
        ds = src.dataset(runs[rid]["dataset_id"])
        step = Timeframe(ds["spec"]["timeframe"]).delta if ds else None
        pts = src.equity(rid)
        for t, v in pts:
            if v is None or not math.isfinite(v):
                out.append(_f("sim_equity", f"{rid}@{t.isoformat()}", "non-finite equity", "critical", "invalid"))
        if step:
            gaps = sum(1 for (a, _), (b, _) in zip(pts, pts[1:]) if b - a != step)
            if gaps:
                out.append(_f("sim_equity", rid, f"{gaps} irregular steps in the equity series", "warning",
                              "incomplete"))


def _check_outcomes_and_observations(src: AnalyticsSource, runs: dict, out: list) -> None:
    for r in src.outcome_rows():
        rec = f"{r['run_id']}/{r['setup_id']}/h{r['horizon']}"
        if r["run_id"] not in runs:
            out.append(_f("outcome_observations", rec, "orphan: run missing", "critical", "orphaned"))
        has_values = bool(r["payload"].get("values"))
        if r["status"] == "valid" and not has_values:
            out.append(_f("outcome_observations", rec, "valid without values", "critical", "invalid"))
        if r["status"] != "valid" and has_values:
            out.append(_f("outcome_observations", rec, f"{r['status']} with values", "critical", "invalid"))
    for rid in sorted(src.run_ids_of("replay_observations") - set(runs)):
        out.append(_f("replay_observations", rid, "orphan: run missing", "critical", "orphaned"))


# ---------------------------------------------------------------------- live
def _check_setups(src: AnalyticsSource, out: list) -> None:
    ids = {s["setup_id"] for s in src.live_setups()}
    ev: dict[str, list[dict]] = {}
    for e in src.setup_events():
        if e["setup_id"] not in ids:
            out.append(_f("setup_events", f"{e['setup_id']}:{e['status']}", "orphan: setup missing", "critical",
                          "orphaned"))
        ev.setdefault(e["setup_id"], []).append(e)
    for sid in sorted(ids):
        es = sorted(ev.get(sid, []), key=lambda e: (e["at_ms"], _ORDER.get(e["status"], 9)))
        sts = [e["status"] for e in es]
        if "candidate" not in sts:
            out.append(_f("setups", sid, "no candidate event", "critical", "invalid"))
        if len([s for s in sts if s in TERMINAL]) > 1:
            out.append(_f("setups", sid, "more than one terminal event", "critical", "invalid"))
        seen_terminal = False
        for s in sts:
            if seen_terminal:
                out.append(_f("setups", sid, f"transition '{s}' after a terminal event", "critical", "invalid"))
                break
            seen_terminal = s in TERMINAL
        ranks = [_ORDER.get(s, 9) for s in sts]
        if ranks != sorted(ranks) or len({e["at_ms"] for e in es}) != len(es):
            out.append(_f("setups", sid, "transition times not strictly increasing in lifecycle order", "critical",
                          "invalid"))


def _check_conflicts(src: AnalyticsSource, out: list) -> None:
    for table, rows in src.conflicts().items():
        for r in rows:
            out.append(_f(table, r["id"], "recorded conflict (a later computation disagreed)", "warning",
                          "inconsistent"))
    for table in CONFLICT_TABLES:
        if not src.triggers_of(table):
            out.append(_f(table, "*", "no append-only triggers on this audit table (decision D3: diagnose only)",
                          "warning", "integrity"))


def _check_monitor(src: AnalyticsSource, out: list) -> None:
    runs = src.monitor_runs()
    for r in runs:
        if r["status"] in ("failed", "abandoned"):
            out.append(_f("monitor_runs", r["run_id"], f"run {r['status']}", "warning", "incomplete"))
        if r["status"] == "running":
            last = r["last_heartbeat_at"] or r["started_at"]
            newer = [x for x in runs if x["started_at"] > last and x["run_id"] != r["run_id"]]
            if newer:
                out.append(_f("monitor_runs", r["run_id"], "status running, but a newer run started after its last "
                              "heartbeat (stale, not closed)", "warning", "incomplete"))
    for e in src.monitor_events():
        if e["persisted"] == "failed":
            out.append(_f("monitor_events", f"{e['run_id']}:{e['symbol']}/{e['timeframe']}@{e['as_of_ms']}",
                          "persistence failed", "info", "incomplete"))
        elif e["status"] in {s.value for s in FAILURE_STATUSES}:
            out.append(_f("monitor_events", f"{e['run_id']}:{e['symbol']}/{e['timeframe']}@{e['as_of_ms']}",
                          f"unit status {e['status']}", "warning", "incomplete"))


def data_quality(src: AnalyticsSource, current_versions: dict, *, check_trade_windows: bool = True) -> dict:
    runs = {r["run_id"]: r for r in src.list_runs()}
    findings: list[dict] = []
    _check_trades(src, runs, findings)
    if check_trade_windows:
        _check_trade_windows(src, runs, findings)
    _check_equity(src, runs, findings)
    _check_outcomes_and_observations(src, runs, findings)
    for r in runs.values():
        if dict(r["versions"]) != dict(current_versions):
            findings.append(_f("research_runs", r["run_id"], "version_mismatch with current component versions",
                               "info", "incomplete"))
    _check_setups(src, findings)
    _check_conflicts(src, findings)
    _check_monitor(src, findings)
    snapshot_quality = {t: src.snapshot_quality(t) for t in ("regime_snapshots", "structure_snapshots",
                                                             "setup_snapshots")}
    for t, q in snapshot_quality.items():
        bad = sum(v for k, v in q.items() if k != "valid")
        if bad:
            findings.append(_f(t, "*", f"{bad} snapshots with data_quality != valid ({q})", "info", "incomplete"))
    findings.sort(key=lambda f: ({"critical": 0, "warning": 1, "info": 2}[f["severity"]], f["table"], f["record"],
                                 f["problem"]))
    tables = sorted({f["table"] for f in findings} | {"sim_trades", "sim_equity", "outcome_observations",
                                                       "replay_observations", "setups", "setup_events",
                                                       "monitor_runs", "monitor_events", *CONFLICT_TABLES})
    summary = {}
    for t in tables:
        fs = [f for f in findings if f["table"] == t]
        bad_records = {f["record"] for f in fs if f["severity"] == "critical" and f["record"] != "*"}
        total = src.table_count(t) if t in src.table_names() else 0
        summary[t] = {"total": total, "valid": max(0, total - len(bad_records)),
                      **{c: len({f["record"] for f in fs if f["category"] == c})
                         for c in ("invalid", "inconsistent", "orphaned", "duplicated", "incomplete", "integrity")}}
    return {"findings": findings, "summary": summary,
            "counts": {s: sum(1 for f in findings if f["severity"] == s) for s in ("critical", "warning", "info")},
            "snapshot_quality": snapshot_quality,
            "note": "diagnostic only: nothing was corrected or written"}


# ------------------------------------------------------------- 5.13 monitor
def monitor_summary(src: AnalyticsSource) -> dict:
    runs = src.monitor_runs()
    events = src.monitor_events()
    by_status: dict[str, int] = {}
    for r in runs:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    errs = [json.loads(r["summary_json"]).get("cycles_with_errors") for r in runs if r["summary_json"]]
    ev_status: dict[str, int] = {}
    for e in events:
        ev_status[e["status"]] = ev_status.get(e["status"], 0) + 1
    failed_persist = sum(1 for e in events if e["persisted"] == "failed")
    kinds: dict[str, dict[str, int]] = {}
    for e in src.structure_events():
        k = f"{e['symbol']}/{e['timeframe']}"
        kinds.setdefault(k, {})
        kinds[k][e["kind"]] = kinds[k].get(e["kind"], 0) + 1
    return {"runs": stats.value(len(runs)), "runs_by_status": dict(sorted(by_status.items())),
            "cycles_with_errors": stats.value(sum(x for x in errs if isinstance(x, int))) if errs
            else stats.na("no_finished_runs"),
            "event_status": dict(sorted(ev_status.items())),
            "persist_failed_share": stats.ratio(failed_persist, len(events), "monitor_events"),
            "median_duration_ms": stats.median([e["duration_ms"] for e in events if e["duration_ms"] is not None],
                                               "monitor_events"),
            "structure_events": {k: dict(sorted(v.items())) for k, v in sorted(kinds.items())}}
