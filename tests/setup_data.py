"""Hand-built paths for setup tests (expected lifecycles are derived by hand in
tests/test_setup.py comments). Warmup 200 calm bars (h=101, l=99, c=100) gives
ATR ~2 and enough contiguous history for structure at the oldest replayed bar."""
from __future__ import annotations

from datetime import datetime, timezone

from app.research.setup.config import SetupConfig
from app.research.setup.service import SetupService
from app.research.structure.service import StructureService
from tests.structure_data import ETH, M15, PATH_A, PATH_B, ROOT, T0, bar_close, candles, cfg as structure_cfg

W = 200
SETUP_CONFIG = ROOT / "config" / "setup_v1.json"
EVAL = datetime(2026, 9, 24, tzinfo=timezone.utc)       # fixed wall clock for deterministic JSON

# PATH_A + departure (bar 15 closes 111.5 > 108 + zone), return (bar 16 low 108.5), resumption (bar 17 > 111.6)
PATH_P = PATH_A[:15] + [(112, 108.8, 111.5), (111.6, 108.5, 109), (112, 108.9, 111.8)]
# PATH_A + price never comes back to 108 after departing
PATH_N = PATH_A[:15] + [(112, 108.8, 111.5), (115, 111.2, 114.5), (118, 114.2, 117.5)]
PATH_S1 = PATH_B + [(104, 99, 99.5)]          # sweep of EQH 106 at bar 9, bar 11 closes below sweep-bar low 100.5
PATH_S2 = PATH_B + [(107.5, 102.5, 107)]      # bar 11 closes above the sweep extreme 106.5
PATH_R = [(103, 100, 102), (106, 101, 105), (110, 104, 108), (109, 105, 106), (107, 103, 104),   # H@2 = 110
          (105, 101, 102), (103, 100, 101), (104, 101, 103), (106, 102, 105), (108, 104, 107),   # L@6 = 100
          (109.8, 105.5, 106), (107, 104.5, 105)]    # bar 10 rejects 110 without piercing; bar 11 < 105.5


def scfg(**changes) -> SetupConfig:
    c = SetupConfig.from_file(SETUP_CONFIG)
    return c.with_(**changes) if changes else c


def service(**changes) -> SetupService:
    return SetupService(None, StructureService(None, structure_cfg()), scfg(**changes))


def evaluate(path, last, *, regime=None, warmup=W, extra=(), **changes):
    cs = candles(path, warmup=warmup) + list(extra)
    return service(**changes).compute(cs, ETH, M15, bar_close(last, warmup=warmup), regime, EVAL)


def idx(t, warmup=W):
    """path index of the bar whose CLOSE is t"""
    return (t - T0) // M15.delta - warmup - 1


def life(setup, warmup=W):
    return [(tr.status.value, idx(tr.at, warmup), tr.reason) for tr in setup.transitions]


def by_type(snap, typ):
    return [s for s in snap.setups if s.setup_type.value == typ]
