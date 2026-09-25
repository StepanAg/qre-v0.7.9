"""Trade excursions (TZ 5.4), computed read-only from closed candles.

Window: bars with open_time in [entry_time, exit_time). Resolution is one OHLC bar:
the exit bar's high/low may include movement after the exit, so MAE/MFE are upper
bounds (flag bar_resolution_upper_bound). Bid/ask are not recorded by QRE."""
from __future__ import annotations

from typing import Sequence

from app.domain.market import Candle, Timeframe
from app.domain.research_lab import SimulatedTrade
from app.research.lab import stats


def trade_excursions(t: SimulatedTrade, candles: Sequence[Candle], tf: Timeframe) -> dict:
    window = [c for c in candles if t.entry_time <= c.open_time < t.exit_time]
    expected = int((t.exit_time - t.entry_time) / tf.delta)
    contiguous = len(window) == expected and all(
        b.open_time - a.open_time == tf.delta for a, b in zip(window, window[1:])) and \
        (not window or window[0].open_time == t.entry_time)
    base = {"trade_no": t.trade_no, "symbol": t.symbol, "direction": t.direction, "bars": len(window),
            "bar_resolution_upper_bound": True}
    if not window or not contiguous:
        return {**base, "status": "not_available", "reason": "gap_in_trade_window"}
    long = t.direction == "bullish"
    e = t.entry_price
    fav = [(float(c.high) - e) if long else (e - float(c.low)) for c in window]
    adv = [(e - float(c.low)) if long else (float(c.high) - e) for c in window]
    mfe, mae = max(0.0, max(fav)), max(0.0, max(adv))
    unit = abs(e - t.stop)
    hi, lo = max(float(c.high) for c in window), min(float(c.low) for c in window)
    b_mfe, b_mae = fav.index(max(fav)) + 1, adv.index(max(adv)) + 1
    sign = 1 if long else -1
    out = {**base, "status": "valid",
           "mfe_price": mfe, "mae_price": mae, "mfe_pct": mfe / e, "mae_pct": mae / e,
           "mfe_r": stats.value(mfe / unit) if unit else stats.na("zero_stop_distance"),
           "mae_r": stats.value(mae / unit) if unit else stats.na("zero_stop_distance"),
           "bars_to_mfe": b_mfe, "bars_to_mae": b_mae,
           "adverse_first": "ambiguous" if b_mae == b_mfe else b_mae < b_mfe,
           "capture_ratio": stats.value(sign * (t.exit_price - e) / mfe) if mfe > 0
           else stats.na("zero_denominator:mfe"),
           "entry_efficiency": stats.value(1 - mae / (mae + mfe)) if mae + mfe > 0
           else stats.na("zero_denominator:mae_plus_mfe"),
           "exit_efficiency": stats.value(((t.exit_price - lo) if long else (hi - t.exit_price)) / (hi - lo))
           if hi > lo else stats.na("zero_denominator:bar_range")}
    return out
