"""Rendering of analytics envelopes: canonical JSON, flat CSV, readable text.
Deterministic: sorted keys, fixed CSV columns, no wall clock."""
from __future__ import annotations

import csv
import io
import json
from typing import Any

ENVELOPE_KEYS = ("source", "filters", "as_of", "versions", "warnings", "data")


def cell(v: Any) -> str:
    """Metric -> text: value, or 'N/A: reason' (never an invented number)."""
    if isinstance(v, dict) and "value" in v and len(v) == 1:
        v = v["value"]
    if isinstance(v, dict) and v.get("status") == "not_available":
        return f"N/A: {v['reason']}"
    if isinstance(v, float):
        return f"{v:.6g}"
    if isinstance(v, (dict, list)):
        return json.dumps(v, sort_keys=True, default=str)
    return "" if v is None else str(v)


def to_json(env: dict) -> str:
    missing = [k for k in ENVELOPE_KEYS if k not in env]
    if missing:
        raise ValueError(f"envelope misses {missing}")
    return json.dumps(env, sort_keys=True, indent=2, allow_nan=False, default=str)


def flat_rows(command: str, data: Any) -> tuple[list[str], list[dict]]:
    """Tabular commands only; anything nested must use --format json."""
    if command == "trades":
        rows = []
        for t in data["trades"]:
            e = t.get("excursions") or {}
            rows.append({**{k: t[k] for k in sorted(t) if k != "excursions"},
                         **{f"exc_{k}": cell(e.get(k)) for k in ("status", "mfe_pct", "mae_pct", "capture_ratio",
                                                                 "entry_efficiency", "exit_efficiency")}})
        return (list(rows[0]) if rows else ["trade_no"]), rows
    if command == "periods":
        cols = ["period", "by", "trades", "net_pnl", "return", "median_r", "expectancy", "profit_factor",
                "max_drawdown", "fees", "funding", "slippage"]
        return cols, [{c: cell(r[c]) for c in cols} for r in data]
    if command in ("exits",) or (command in ("setups", "regimes", "horizons") and isinstance(data, dict)):
        groups = data if command == "exits" else next((v for k, v in data.items() if k.startswith("by_")), {})
        if command == "setups" and "by_setup_type" in data:
            groups = {f"{dim}={k}": v for dim in ("by_setup_type", "by_direction", "by_symbol")
                      for k, v in data[dim].items()}
        metric_cols = sorted({k for g in groups.values() for k in g})
        return ["group", *metric_cols], [{"group": k, **{c: cell(g.get(c)) for c in metric_cols}}
                                         for k, g in groups.items()]
    if command == "funnel" and "steps" in data:
        cols = ["step", "count", "conversion", "rejection"]
        return cols, [{c: cell(s.get(c)) for c in cols} for s in data["steps"]]
    if command == "data-quality":
        cols = ["severity", "table", "record", "category", "problem"]
        return cols, [{c: f[c] for c in cols} for f in data["findings"]]
    raise ValueError(f"'{command}' has nested output; use --format json")


def to_csv(command: str, env: dict) -> str:
    cols, rows = flat_rows(command, env["data"])
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n", extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({c: cell(r.get(c)) for c in cols})
    return buf.getvalue()


def to_text(command: str, env: dict) -> str:
    lines = [f"analytics {command}  source={env['source']}  as_of={env['as_of'] or '-'}"]
    if env["filters"]:
        lines.append("filters: " + ", ".join(f"{k}={v}" for k, v in sorted(env["filters"].items())))
    for w in env["warnings"]:
        lines.append(f"WARNING: {w}" if not w.startswith("WARNING") else w)
    _walk(env["data"], lines, 0)
    return "\n".join(lines)


def _walk(d: Any, lines: list, depth: int) -> None:
    pad = "  " * depth
    if isinstance(d, dict) and not ("value" in d and len(d) == 1) and d.get("status") != "not_available":
        for k in sorted(d, key=str):
            v = d[k]
            if isinstance(v, (dict, list)) and not (isinstance(v, dict) and (("value" in v and len(v) == 1)
                                                                            or v.get("status") == "not_available")):
                lines.append(f"{pad}{k}:")
                _walk(v, lines, depth + 1)
            else:
                lines.append(f"{pad}{k:<28} {cell(v)}")
    elif isinstance(d, list):
        for i, v in enumerate(d):
            if isinstance(v, (dict, list)):
                lines.append(f"{pad}- [{i}]")
                _walk(v, lines, depth + 1)
            else:
                lines.append(f"{pad}- {cell(v)}")
    else:
        lines.append(f"{pad}{cell(d)}")
