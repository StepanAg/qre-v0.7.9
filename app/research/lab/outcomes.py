"""Setup Outcome Analysis - the ONLY layer that looks at bars after a setup.

It never feeds back into setup detection. A setup is not a trade: outcomes are
measured from the anchor close (candidate or confirmation), direction-signed:
  long : favorable = high - P0, adverse = P0 - low, return = +(C_h/P0 - 1)
  short: favorable = P0 - low,  adverse = high - P0, return = -(C_h/P0 - 1)
A horizon past the dataset end is CENSORED, a gap inside it INCOMPLETE; neither is a zero."""
from __future__ import annotations

import hashlib
from typing import Sequence

from app.domain.enums import OutcomeStatus, StructureDirection
from app.domain.market import Candle, Timeframe
from app.domain.research_lab import OutcomeObservation, canonical
from app.research.lab import stats
from app.research.lab.config import ResearchConfig
from app.research.lab.replay import ReplayResult


def outcome_run_id(replay_run_id: str, cfg: ResearchConfig) -> str:
    return "oc_" + hashlib.sha256(canonical({"replay": replay_run_id, "cfg": cfg.config_hash}).encode()).hexdigest()[:20]


def analyze(replay: ReplayResult, candles_by_symbol: dict[str, Sequence[Candle]], tf: Timeframe,
            cfg: ResearchConfig) -> tuple[list[OutcomeObservation], dict]:
    index = {sym: {c.close_time: k for k, c in enumerate(cs)} for sym, cs in candles_by_symbol.items()}
    out: list[OutcomeObservation] = []
    skipped = {"not_confirmed": 0}
    for o in replay.observations:
        s = o.setup
        anchor_t = s.setup_time if cfg.outcome_anchor == "candidate" else s.confirmed_at
        if anchor_t is None:
            skipped["not_confirmed"] += 1
            continue
        cs = candles_by_symbol.get(s.symbol, [])
        i = index.get(s.symbol, {}).get(anchor_t)
        sign = 1 if s.direction is StructureDirection.BULLISH else -1
        atr = dict(s.evidence).get("atr")
        for h in cfg.outcome_horizons:
            base = dict(setup_id=s.setup_id, symbol=s.symbol, setup_type=s.setup_type.value,
                        direction=s.direction.value, anchor=cfg.outcome_anchor, anchor_time=anchor_t, horizon=h)
            if i is None:
                out.append(OutcomeObservation(**base, status=OutcomeStatus.NO_ANCHOR, reason="ANCHOR_BAR_NOT_IN_DATASET"))
                continue
            last = min(i + h, len(cs) - 1)         # a gap inside the AVAILABLE part wins over end-of-data
            if cs[last].open_time - cs[i].open_time != tf.delta * (last - i):
                out.append(OutcomeObservation(**base, status=OutcomeStatus.INCOMPLETE, reason="GAP_IN_HORIZON"))
                continue
            if i + h >= len(cs):
                out.append(OutcomeObservation(**base, status=OutcomeStatus.CENSORED,
                                              reason=f"END_OF_DATA:{len(cs) - 1 - i}_of_{h}_bars"))
                continue
            out.append(OutcomeObservation(**base, status=OutcomeStatus.VALID, reason="ok",
                                          values=_measure(cs, i, h, sign, atr, s.key_level, cfg)))
    return out, {"skipped": skipped}


def _measure(cs: Sequence[Candle], i: int, h: int, sign: int, atr, level: float, cfg: ResearchConfig) -> dict:
    p0 = float(cs[i].close)
    fav, adv = [], []
    for k in range(1, h + 1):
        hi, lo = float(cs[i + k].high), float(cs[i + k].low)
        fav.append((hi - p0) if sign > 0 else (p0 - lo))
        adv.append((p0 - lo) if sign > 0 else (hi - p0))
    mfe_abs, mae_abs = max(0.0, max(fav)), max(0.0, max(adv))
    ch = float(cs[i + h].close)
    v = {"forward_return": sign * (ch / p0 - 1), "mfe": mfe_abs / p0, "mae": mae_abs / p0,
         "bars_to_mfe": fav.index(max(fav)) + 1, "bars_to_mae": adv.index(max(adv)) + 1,
         "entry_reference": p0, "exit_reference": ch}
    if atr:
        v["mfe_atr"], v["mae_atr"] = mfe_abs / atr, mae_abs / atr
        v["close_beyond_level_atr"] = sign * (ch - level) / atr
        for t in cfg.outcome_thresholds_atr:
            v[f"fav_{t:g}atr_hit"] = mfe_abs >= t * atr
            v[f"adv_{t:g}atr_hit"] = mae_abs >= t * atr
            first = "none"
            for f, a in zip(fav, adv):
                fh, ah = f >= t * atr, a >= t * atr
                if fh and ah:
                    first = "ambiguous"        # same bar, order unknown
                    break
                if fh or ah:
                    first = "favorable" if fh else "adverse"
                    break
            v[f"first_{t:g}atr"] = first
    return v


def summarize(obs: Sequence[OutcomeObservation], cfg: ResearchConfig) -> dict:
    out = {}
    for h in cfg.outcome_horizons:
        rows = [o for o in obs if o.horizon == h]
        valid = [o for o in rows if o.status is OutcomeStatus.VALID]
        counts = {st.value: sum(1 for o in rows if o.status is st) for st in OutcomeStatus}
        col = lambda k: [o.values[k] for o in valid if o.values.get(k) is not None]  # noqa: E731
        d = {"counts": counts,
             "forward_return": {"mean": stats.mean(col("forward_return"), "observations"),
                                "median": stats.median(col("forward_return"), "observations")},
             "mfe": {"mean": stats.mean(col("mfe"), "observations"), "median": stats.median(col("mfe"), "observations")},
             "mae": {"mean": stats.mean(col("mae"), "observations"), "median": stats.median(col("mae"), "observations")}}
        for t in cfg.outcome_thresholds_atr:
            hits = col(f"fav_{t:g}atr_hit")
            d[f"fav_{t:g}atr_hit_rate"] = stats.ratio(sum(1 for x in hits if x), len(hits), "observations_with_atr")
            firsts = col(f"first_{t:g}atr")
            d[f"first_{t:g}atr"] = {k: sum(1 for x in firsts if x == k) for k in
                                    ("favorable", "adverse", "ambiguous", "none")}
        out[str(h)] = d
    return out
