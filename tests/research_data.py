"""Research fixtures. PATH_BT = PATH_P (breakout confirmed @16 close 109, pullback confirmed @17)
followed by known bars, so forward outcomes and simulated fills can be computed by hand."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D

from app.domain.market import Timeframe
from app.research.lab.config import BacktestConfig, ResearchConfig
from app.research.lab.pipeline import ResearchPipeline
from app.storage.research import SQLiteResearchStore
from tests.fakes import TempDB
from tests.setup_data import PATH_P, W, service
from tests.structure_data import ROOT, T0
from tests.structure_data import bar_close as _bar_close
from tests.structure_data import bar_open as _bar_open
from tests.structure_data import candles as _candles


def candles(path, *, warmup=W, **kw):
    """Setup-grade fixtures: 200 calm warm-up bars by default (setups need ~137 contiguous bars)."""
    return _candles(path, warmup=warmup, **kw)


def bar_open(i, *, warmup=W, **kw):
    return _bar_open(i, warmup=warmup, **kw)


def bar_close(i, *, warmup=W, **kw):
    return _bar_close(i, warmup=warmup, **kw)

M15 = Timeframe.M15
RCFG = ROOT / "config" / "research_v1.json"
BCFG = ROOT / "config" / "backtest_v1.json"
# bars 17..21 after the breakout confirmation at bar 16 (close 109):
#   17 (112, 108.9, 111.8)  18 (113, 110, 112.5)  19 (112.8, 109.5, 110)  20 (111, 107, 108)  21 (109, 106.5, 107)
PATH_BT = PATH_P + [(113, 110, 112.5), (112.8, 109.5, 110), (111, 107, 108), (109, 106.5, 107)]
LAST = len(PATH_BT) - 1                        # 21
NO_COSTS_BUT_FEES = {"taker_fee_bps": 5.5, "maker_fee_bps": 2.0, "slippage_bps": 0, "funding_mode": "none",
                     "assumed_funding_rate_8h": 0.0001}


def rcfg(**ch) -> ResearchConfig:
    c = ResearchConfig.from_file(RCFG)
    return c.with_(**ch) if ch else c


def bcfg(**ch) -> BacktestConfig:
    base = {"stop_buffer_atr": 0, "setup_types": ["breakout"], "costs": NO_COSTS_BUT_FEES}
    return BacktestConfig.from_file(BCFG).with_(**{**base, **ch})


def with_bar(cs, path_index: int, **ohlc):
    """Replace OHLC of one bar (e.g. a gap open) keeping it a valid candle."""
    k = W + path_index
    c = cs[k]
    cs = list(cs)
    cs[k] = replace(c, **{f: D(str(v)) for f, v in ohlc.items()})
    return cs


class Lab:
    """Temp DB with candles + a pipeline wired like the CLI."""

    def __init__(self, path=PATH_BT, *, warmup=W, cs=None, research=None) -> None:
        self.db = TempDB()
        self.cs = cs if cs is not None else candles(path, warmup=warmup)
        self.db.store.upsert_candles(self.cs, "t")
        self.store = SQLiteResearchStore(self.db.writer)
        svc = service()
        svc.market = self.db.store
        self.pipe = ResearchPipeline(self.db.store, svc, research or rcfg(), self.store)
        self.start, self.end = self.cs[0].open_time, self.cs[-1].close_time

    def dataset(self):
        return self.pipe.build_dataset(["ETHUSDT"], M15, self.start, self.end)[0]

    def replay(self):
        ds = self.dataset()
        return self.pipe.replay(ds.dataset_id)[0]

    def close(self):
        self.db.close()
