"""`python -m app data ...` - composition root for market-data ingestion.
Wiring only: builds transport/client/provider/store and prints reports."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

from app.config.settings import Settings, load_settings
from app.data.backfill import BackfillService, plan_history
from app.data.bybit.client import BybitRestClient
from app.data.bybit.provider import BybitMarketDataProvider
from app.data.http import RequestExecutor, RequestPacer, RetryPolicy, UrllibTransport
from app.data.metrics import IngestionMetrics
from app.data.quality import find_gaps
from app.domain.enums import Category
from app.domain.market import Symbol, Timeframe
from app.storage.database import bootstrap
from app.storage.market_data import SQLiteMarketDataStore
from app.storage.writer import SerializedWriter


def build(settings: Settings, transport=None):
    metrics = IngestionMetrics()
    ex = RequestExecutor(transport or UrllibTransport(), timeout_s=float(settings.http_timeout_s),
                         policy=RetryPolicy(max_attempts=settings.http_max_attempts),
                         pacer=RequestPacer(settings.http_min_interval_ms / 1000), metrics=metrics)
    provider = BybitMarketDataProvider(BybitRestClient(settings.market_data_rest_url, ex),
                                       kline_page_limit=settings.kline_page_limit, metrics=metrics)
    store = SQLiteMarketDataStore(SerializedWriter(bootstrap(settings.db_path, shared=True)))
    return BackfillService(provider, store, store, metrics=metrics), store, metrics


def _ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise SystemExit(f"timestamp {s!r} must include a timezone, e.g. 2026-01-01T00:00+00:00")
    return dt.astimezone(timezone.utc)


def _range(a: argparse.Namespace, tf: Timeframe) -> tuple[datetime, datetime]:
    end = _ts(a.end) if a.end else datetime.now(timezone.utc)
    if a.for_features is not None:
        # history length is owned by the feature requirements, not by the data layer
        from app.research.service import FeatureService
        per_value = FeatureService(store=None).bars_per_value(a.for_features or None)  # type: ignore[arg-type]
        p = plan_history(tf, a.bars or 1, per_value, end)
        print(p.describe())
        return p.start, p.end
    if a.start:
        return _ts(a.start), end
    if a.bars:
        return end - tf.delta * a.bars, end
    return end - timedelta(days=a.days), end


def cmd(a: argparse.Namespace) -> int:
    s = load_settings()
    svc, store, metrics = build(s)
    if a.data_cmd == "instruments":
        n = svc.sync_instruments(Category(a.category), a.symbols or list(s.symbols))
        print(f"instruments stored: {n}")
    elif a.data_cmd == "backfill":
        tfs = [Timeframe.parse(a.tf)] if a.tf else list(s.timeframes)
        symbols = a.symbols or list(s.symbols)
        svc.sync_instruments(Category.LINEAR, symbols)
        for name in symbols:
            for tf in tfs:
                start, end = _range(a, tf)
                r = svc.backfill(Symbol(name), tf, start, end)
                print(f"{name} {tf.value} [{r.start:%Y-%m-%d %H:%M} .. {r.end:%Y-%m-%d %H:%M}) {r.status}: "
                      f"pages={r.pages} inserted={r.inserted} unchanged={r.unchanged} gaps={len(r.gaps)} "
                      f"invalid={r.invalid} conflicts={r.conflicts} open_excluded={r.open_excluded}")
                for g in r.gaps[:10]:
                    print(f"   gap {g}")
    elif a.data_cmd == "funding":
        end = datetime.now(timezone.utc)
        for name in a.symbols or list(s.symbols):
            run = svc.backfill_funding(Symbol(name), end - timedelta(days=a.days), end)
            print(f"{name} funding {run.status}: inserted={run.inserted} unchanged={run.unchanged}")
    elif a.data_cmd == "status":
        print(json.dumps({"coverage": store.coverage(), "recent_runs": store.runs(10)}, indent=2, default=str))
        return 0
    elif a.data_cmd == "gaps":
        tf = Timeframe.parse(a.tf)
        sym = Symbol(a.symbol)
        times = store.candle_times(sym, tf, datetime(2009, 1, 1, tzinfo=timezone.utc), datetime.now(timezone.utc))
        gaps = find_gaps(times, tf)
        print(f"{a.symbol} {tf.value}: {len(times)} candles, {len(gaps)} gaps")
        for g in gaps:
            print(f"  {g}")
        return 0
    elif a.data_cmd == "smoke":
        t = svc.provider.server_time()
        (btc,) = svc.provider.instruments(Category.LINEAR, ["BTCUSDT"])
        b = svc.provider.candles(Symbol("BTCUSDT"), Timeframe.H1, t - timedelta(hours=6), t + timedelta(hours=1))
        print(f"server_time={t.isoformat()} BTCUSDT tick={btc.tick_size} closed_1h_bars={len(b.closed)} "
              f"issues={len(b.issues)}")
    print("metrics:", json.dumps(metrics.snapshot(), indent=2))
    return 0


def register(sub) -> None:
    d = sub.add_parser("data", help="market data: instruments, backfill, funding, status, gaps, smoke")
    ds = d.add_subparsers(dest="data_cmd", required=True)
    i = ds.add_parser("instruments", help="fetch and store instrument rules")
    i.add_argument("--category", default="linear", choices=["linear", "spot"])
    i.add_argument("--symbols", nargs="*")
    b = ds.add_parser("backfill", help="download closed candles (idempotent)")
    b.add_argument("--symbols", nargs="*")
    b.add_argument("--tf", help="1m,5m,15m,1h,4h,1d (default: TIMEFRAMES)")
    b.add_argument("--days", type=int, default=30)
    b.add_argument("--bars", type=int, help="usable bars ending at --end/now")
    b.add_argument("--for-features", nargs="*", metavar="NAME",
                   help="load exactly the history these features need for --bars usable points "
                        "(no names = whole catalog), e.g. --for-features ema_200 atr_14")
    b.add_argument("--start")
    b.add_argument("--end")
    f = ds.add_parser("funding", help="download funding rate history (linear)")
    f.add_argument("--symbols", nargs="*")
    f.add_argument("--days", type=int, default=30)
    ds.add_parser("status", help="stored coverage and recent runs")
    g = ds.add_parser("gaps", help="gap report for stored candles")
    g.add_argument("--symbol", required=True)
    g.add_argument("--tf", required=True)
    ds.add_parser("smoke", help="real Bybit public API smoke test (network)")
    d.set_defaults(fn=cmd)
