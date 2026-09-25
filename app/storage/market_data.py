"""SQLite implementation of MarketDataStore (read) and MarketDataSink (write).
Structurally implements the ports in app.data.ports; storage does not import data."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Sequence

from app.core.errors import StorageError
from app.domain.enums import Category
from app.domain.market import Candle, FundingRate, InstrumentInfo, Symbol, Timeframe
from app.storage.writer import SerializedWriter

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MS = timedelta(milliseconds=1)


def _ms(ts: datetime) -> int:
    return (ts - _EPOCH) // _MS


def _dt(ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=ms)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _s(v) -> str | None:
    return None if v is None else str(v)


def _D(v) -> Decimal | None:
    return None if v is None else Decimal(v)


@dataclass(frozen=True)
class StoredIssue:
    kind: str
    detail: str
    at: datetime | None = None
    raw: str = ""


@dataclass(frozen=True)
class StoreUpsert:
    inserted: int
    unchanged: int
    conflicts: tuple[StoredIssue, ...] = ()


class SQLiteMarketDataStore:
    def __init__(self, writer: SerializedWriter) -> None:
        self.writer = writer

    # ======================================================= write side
    def upsert_candles(self, candles: Sequence[Candle], source: str) -> StoreUpsert:
        """Idempotent. Identical candle -> unchanged. Same key with different
        values -> conflict (stored candle kept, never silently overwritten)."""
        if any(not c.is_closed for c in candles):
            raise StorageError("refusing to persist an open (unclosed) candle")
        if not candles:
            return StoreUpsert(0, 0)
        groups: dict[tuple, list[Candle]] = {}
        for c in candles:
            groups.setdefault((c.symbol.category.value, c.symbol.name, c.timeframe.value), []).append(c)
        inserted = unchanged = 0
        conflicts: list[StoredIssue] = []
        now = _now()
        with self.writer.write() as db:
            for (cat, sym, tf), items in groups.items():
                lo, hi = min(_ms(c.open_time) for c in items), max(_ms(c.open_time) for c in items)
                existing = {r[0]: r[1:] for r in db.execute(
                    "SELECT open_time_ms, open, high, low, close, volume, turnover FROM candles "
                    "WHERE category=? AND symbol=? AND timeframe=? AND open_time_ms BETWEEN ? AND ?",
                    (cat, sym, tf, lo, hi))}
                new_rows = []
                for c in items:
                    t = _ms(c.open_time)
                    vals = (c.open, c.high, c.low, c.close, c.volume, c.turnover)
                    if t in existing:
                        if tuple(Decimal(x) for x in existing[t]) == vals:
                            unchanged += 1
                        else:
                            conflicts.append(StoredIssue(
                                "stored_conflict",
                                f"{sym} {tf}: fetched candle differs from stored one; stored kept",
                                c.open_time, f"stored={existing[t]} fetched={tuple(map(str, vals))}"))
                        continue
                    existing[t] = tuple(str(v) for v in vals)
                    new_rows.append((cat, sym, tf, t, *map(str, vals), source, now))
                db.executemany("INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", new_rows)
                inserted += len(new_rows)
        return StoreUpsert(inserted, unchanged, tuple(conflicts))

    def upsert_instruments(self, items: Sequence[InstrumentInfo], source: str) -> int:
        """Instrument rules can legitimately change (tick size, status) -> update in place."""
        now = _now()
        rows = [(
            i.symbol.category.value, i.symbol.name, i.base_coin, i.quote_coin, i.settle_coin, i.status,
            i.contract_type, str(i.tick_size), str(i.qty_step), str(i.min_qty), _s(i.min_notional),
            _s(i.max_qty), _s(i.max_market_qty), i.price_scale, _s(i.min_leverage), _s(i.max_leverage),
            _s(i.leverage_step), i.funding_interval_min,
            None if i.launch_time is None else _ms(i.launch_time), source, now) for i in items]
        with self.writer.write() as db:
            db.executemany(
                "INSERT INTO instruments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(category, symbol) DO UPDATE SET base_coin=excluded.base_coin, "
                "quote_coin=excluded.quote_coin, settle_coin=excluded.settle_coin, status=excluded.status, "
                "contract_type=excluded.contract_type, tick_size=excluded.tick_size, "
                "qty_step=excluded.qty_step, min_qty=excluded.min_qty, min_notional=excluded.min_notional, "
                "max_qty=excluded.max_qty, max_market_qty=excluded.max_market_qty, "
                "price_scale=excluded.price_scale, min_leverage=excluded.min_leverage, "
                "max_leverage=excluded.max_leverage, leverage_step=excluded.leverage_step, "
                "funding_interval_min=excluded.funding_interval_min, launch_time_ms=excluded.launch_time_ms, "
                "source=excluded.source, updated_at=excluded.updated_at", rows)
        return len(rows)

    def upsert_funding(self, items: Sequence[FundingRate], source: str) -> StoreUpsert:
        inserted = unchanged = 0
        conflicts: list[StoredIssue] = []
        now = _now()
        with self.writer.write() as db:
            for r in items:
                t = _ms(r.funding_time)
                row = db.execute("SELECT rate FROM funding_rates WHERE category='linear' AND symbol=? "
                                 "AND funding_time_ms=?", (r.symbol.name, t)).fetchone()
                if row is None:
                    db.execute("INSERT INTO funding_rates VALUES ('linear',?,?,?,?,?)",
                               (r.symbol.name, t, str(r.rate), source, now))
                    inserted += 1
                elif Decimal(row[0]) == r.rate:
                    unchanged += 1
                else:
                    conflicts.append(StoredIssue("stored_conflict", f"funding {r.symbol.name}",
                                                 r.funding_time, f"stored={row[0]} fetched={r.rate}"))
        return StoreUpsert(inserted, unchanged, tuple(conflicts))

    def record_run(self, run) -> None:
        with self.writer.write() as db:
            db.execute(
                "INSERT INTO backfill_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET finished_at=excluded.finished_at, "
                "status=excluded.status, pages=excluded.pages, fetched=excluded.fetched, "
                "inserted=excluded.inserted, unchanged=excluded.unchanged, conflicts=excluded.conflicts, "
                "invalid=excluded.invalid, open_excluded=excluded.open_excluded, gaps=excluded.gaps, "
                "error=excluded.error",
                (run.run_id, run.dataset, run.symbol.category.value, run.symbol.name,
                 None if run.timeframe is None else run.timeframe.value,
                 _ms(run.start), _ms(run.end), run.started_at.isoformat(),
                 None if run.finished_at is None else run.finished_at.isoformat(), run.status,
                 run.pages, run.fetched, run.inserted, run.unchanged, run.conflicts, run.invalid,
                 run.open_excluded, run.gaps, run.error))
            db.execute("DELETE FROM data_quality_issues WHERE run_id=?", (run.run_id,))
            db.executemany(
                "INSERT INTO data_quality_issues(run_id, kind, at_ms, detail, raw) VALUES (?,?,?,?,?)",
                [(run.run_id, i.kind, None if i.at is None else _ms(i.at), i.detail, i.raw[:1000])
                 for i in run.issues])

    # ======================================================== read side
    def _candle(self, sym: Symbol, tf: Timeframe, r) -> Candle:
        return Candle(sym, tf, _dt(r[0]), Decimal(r[1]), Decimal(r[2]), Decimal(r[3]), Decimal(r[4]),
                      Decimal(r[5]), Decimal(r[6]), is_closed=True)

    def get_candles(self, symbol: Symbol, timeframe: Timeframe, start: datetime, end: datetime) -> list[Candle]:
        with self.writer.read() as db:
            rows = db.execute(
                "SELECT open_time_ms, open, high, low, close, volume, turnover FROM candles "
                "WHERE category=? AND symbol=? AND timeframe=? AND open_time_ms >= ? AND open_time_ms < ? "
                "ORDER BY open_time_ms",
                (symbol.category.value, symbol.name, timeframe.value, _ms(start), _ms(end))).fetchall()
        return [self._candle(symbol, timeframe, r) for r in rows]

    def last_candles(self, symbol: Symbol, timeframe: Timeframe, end: datetime, count: int) -> list[Candle]:
        with self.writer.read() as db:
            rows = db.execute(
                "SELECT open_time_ms, open, high, low, close, volume, turnover FROM candles "
                "WHERE category=? AND symbol=? AND timeframe=? AND open_time_ms < ? "
                "ORDER BY open_time_ms DESC LIMIT ?",
                (symbol.category.value, symbol.name, timeframe.value, _ms(end), count)).fetchall()
        return [self._candle(symbol, timeframe, r) for r in reversed(rows)]

    def candle_times(self, symbol: Symbol, timeframe: Timeframe, start: datetime, end: datetime) -> list[datetime]:
        with self.writer.read() as db:
            rows = db.execute(
                "SELECT open_time_ms FROM candles WHERE category=? AND symbol=? AND timeframe=? "
                "AND open_time_ms >= ? AND open_time_ms < ? ORDER BY open_time_ms",
                (symbol.category.value, symbol.name, timeframe.value, _ms(start), _ms(end))).fetchall()
        return [_dt(r[0]) for r in rows]

    def candle_count(self, symbol: Symbol, timeframe: Timeframe) -> int:
        with self.writer.read() as db:
            return db.execute("SELECT COUNT(*) FROM candles WHERE category=? AND symbol=? AND timeframe=?",
                              (symbol.category.value, symbol.name, timeframe.value)).fetchone()[0]

    def coverage(self) -> list[dict]:
        with self.writer.read() as db:
            rows = db.execute(
                "SELECT category, symbol, timeframe, COUNT(*), MIN(open_time_ms), MAX(open_time_ms) "
                "FROM candles GROUP BY category, symbol, timeframe ORDER BY 1,2,3").fetchall()
        return [{"category": r[0], "symbol": r[1], "timeframe": r[2], "candles": r[3],
                 "first": _dt(r[4]).isoformat(), "last": _dt(r[5]).isoformat()} for r in rows]

    def instrument(self, symbol: Symbol) -> InstrumentInfo | None:
        with self.writer.read() as db:
            r = db.execute("SELECT * FROM instruments WHERE category=? AND symbol=?",
                           (symbol.category.value, symbol.name)).fetchone()
        if r is None:
            return None
        return InstrumentInfo(
            symbol=Symbol(r["symbol"], Category(r["category"])), tick_size=Decimal(r["tick_size"]),
            qty_step=Decimal(r["qty_step"]), min_qty=Decimal(r["min_qty"]), min_notional=_D(r["min_notional"]),
            base_coin=r["base_coin"], quote_coin=r["quote_coin"], settle_coin=r["settle_coin"],
            status=r["status"], contract_type=r["contract_type"], price_scale=r["price_scale"],
            max_qty=_D(r["max_qty"]), max_market_qty=_D(r["max_market_qty"]),
            min_leverage=_D(r["min_leverage"]), max_leverage=_D(r["max_leverage"]),
            leverage_step=_D(r["leverage_step"]), funding_interval_min=r["funding_interval_min"],
            launch_time=None if r["launch_time_ms"] is None else _dt(r["launch_time_ms"]))

    def funding_rates(self, symbol: Symbol, start: datetime, end: datetime) -> list[FundingRate]:
        with self.writer.read() as db:
            rows = db.execute(
                "SELECT funding_time_ms, rate FROM funding_rates WHERE symbol=? AND funding_time_ms >= ? "
                "AND funding_time_ms < ? ORDER BY funding_time_ms", (symbol.name, _ms(start), _ms(end))).fetchall()
        return [FundingRate(symbol, _dt(r[0]), Decimal(r[1])) for r in rows]

    def runs(self, limit: int = 20) -> list[dict]:
        with self.writer.read() as db:
            rows = db.execute("SELECT * FROM backfill_runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def issues(self, run_id: str) -> list[dict]:
        with self.writer.read() as db:
            return [dict(r) for r in db.execute(
                "SELECT kind, at_ms, detail FROM data_quality_issues WHERE run_id=? ORDER BY id", (run_id,))]
