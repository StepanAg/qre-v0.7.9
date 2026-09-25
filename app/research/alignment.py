"""Multi-timeframe alignment without look-ahead.

A bar of timeframe TF with open time `o` is KNOWN at decision time T only when
o + TF <= T (the bar has closed). For a 15m decision at 15:15 the latest known
1h bar is 14:00-15:00; the 15:00-16:00 bar is invisible until 16:00.
"""
from __future__ import annotations

from datetime import datetime

from app.domain.market import Timeframe


def last_closed_open_time(tf: Timeframe, as_of: datetime) -> datetime:
    """Open time of the most recent bar of `tf` that is closed at `as_of`."""
    return tf.floor(as_of) - tf.delta


def is_known_at(open_time: datetime, tf: Timeframe, as_of: datetime) -> bool:
    return open_time + tf.delta <= as_of
