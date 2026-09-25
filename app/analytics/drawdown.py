"""Drawdown Analyzer (TZ 5.8) over an equity series [(time, equity)] sorted by time.
Episode = from a running peak until equity is back at/above that peak."""
from __future__ import annotations

from datetime import datetime
from typing import Sequence

from app.research.lab import stats


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t else None


def episodes(points: Sequence[tuple[datetime, float]]) -> list[dict]:
    out: list[dict] = []
    if not points:
        return out
    peak_t, peak = points[0]
    cur = None
    for t, v in points[1:]:
        if v >= peak:
            if cur is not None:
                cur["recovery"] = t
                out.append(cur)
                cur = None
            peak_t, peak = t, v
            continue
        if cur is None:
            cur = {"start": peak_t, "peak": peak, "trough_time": t, "trough": v, "recovery": None}
        elif v < cur["trough"]:
            cur["trough_time"], cur["trough"] = t, v
    if cur is not None:
        out.append(cur)
    res = []
    for e in out:
        depth = e["peak"] - e["trough"]
        rec = e["recovery"]
        res.append({
            "start": _iso(e["start"]), "peak": e["peak"], "trough_time": _iso(e["trough_time"]), "trough": e["trough"],
            "recovery": _iso(rec), "depth": depth, "depth_pct": depth / e["peak"] if e["peak"] > 0 else None,
            "time_to_trough_s": (e["trough_time"] - e["start"]).total_seconds(),
            "duration_s": stats.value((rec - e["start"]).total_seconds()) if rec else stats.na("not_recovered"),
            "recovery_duration_s": stats.value((rec - e["trough_time"]).total_seconds()) if rec
            else stats.na("not_recovered")})
    return res


def analyze(points: Sequence[tuple[datetime, float]]) -> dict:
    if len(points) < 2:
        na = stats.na("equity_curve_needs_2_points")
        return {"max_drawdown": na, "max_drawdown_pct": na, "current_drawdown": na, "episodes": [],
                "longest_duration_s": na}
    eps = episodes(points)
    peak = max(v for _, v in points)
    last = points[-1][1]
    deepest = max(eps, key=lambda e: e["depth"]) if eps else None
    durs = [e["duration_s"]["value"] for e in eps if "value" in e["duration_s"]]
    return {"max_drawdown": stats.value(deepest["depth"] if deepest else 0.0),
            "max_drawdown_pct": stats.value(deepest["depth_pct"] if deepest else 0.0),
            "max_drawdown_episode": deepest,
            "current_drawdown": stats.value(peak - last), "peak_equity": stats.value(peak),
            "current_equity": stats.value(last), "episodes": eps,
            "longest_duration_s": stats.value(max(durs)) if durs else stats.na("no_recovered_episode")}


def drawdown_curve(points: Sequence[tuple[datetime, float]]) -> list[dict]:
    out, peak = [], None
    for t, v in points:
        peak = v if peak is None or v > peak else peak
        out.append({"t": t.isoformat(), "equity": v, "drawdown": peak - v,
                    "drawdown_pct": (peak - v) / peak if peak > 0 else None})
    return out
