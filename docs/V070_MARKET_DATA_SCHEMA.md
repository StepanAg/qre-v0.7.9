# QRE v0.7.0 — Market Data Schema (migration `0002_market_data.sql`)

Общие правила: время — INTEGER epoch ms (UTC); Decimal — TEXT; применяется тем же migration runner, что и 0001 (контроль checksum). База Phase 0 обновляется на месте без потери данных (тест `test_phase0_database_upgrades`).

## `candles` (только закрытые бары)
| Колонка | Тип | Примечание |
|---|---|---|
| category | TEXT | CHECK `linear`/`spot` |
| symbol | TEXT | |
| timeframe | TEXT | CHECK `1m,5m,15m,1h,4h,1d` (канонические метки) |
| open_time_ms | INTEGER | начало бара |
| open, high, low, close | TEXT | quote за 1 base |
| volume | TEXT | base coin |
| turnover | TEXT | quote currency |
| source, ingested_at | TEXT | происхождение |

- `PRIMARY KEY (category, symbol, timeframe, open_time_ms)` `WITHOUT ROWID` — одновременно уникальность и индекс для диапазонных запросов.
- Триггер `candles_aligned`: `open_time_ms` должен быть кратен длине таймфрейма.
- Триггер `candles_immutable`: UPDATE запрещён.

## `instruments`
PK `(category, symbol)`. Поля: base/quote/settle coin, status, contract_type, tick_size, qty_step, min_qty, min_notional, max_qty, max_market_qty, price_scale, min/max_leverage, leverage_step, funding_interval_min, launch_time_ms. Поле, которого нет в ответе API для данной категории, хранится как NULL (для spot нет leverage/funding/launch_time). Upsert обновляет строку: правила инструмента законно меняются. **История изменений не хранится** (см. known issues).

## `funding_rates`
PK `(category, symbol, funding_time_ms)`, `rate` TEXT (доля за интервал). Только linear. Неизменяемые (триггер).

## `backfill_runs`
Один прогон = одна строка: dataset, диапазон, статус (`running/ok/ok_with_gaps/completed_with_issues/failed`), pages, fetched, inserted, unchanged, conflicts, invalid, open_excluded, gaps, error.

## `data_quality_issues`
`run_id`, `kind` (`invalid_record`, `conflicting_duplicate`, `stored_conflict`, `out_of_order`, `out_of_range`, `unit_suspect`, `gap`), `at_ms`, `detail`, `raw` (≤1000 символов исходной записи).

## Почему нет таблицы `tickers`
Тикер — моментальный снимок, всегда доступный повторно по API. Сохранять его имеет смысл только как временной ряд (для сканера), а это отдельное решение следующих фаз.

## Research tables (Phase 7, `0008_research.sql`)
| Таблица | Содержимое | Ключ |
|---|---|---|
| research_datasets | манифест датасета (спецификация, fingerprints, версии, сегменты) | dataset_id (хэш) |
| research_runs | replay / outcomes / backtest: конфиг, версии, summary, result_hash | run_id (хэш от родителя и конфига) |
| replay_observations | сетапы, как они были известны | (run_id, setup_id) |
| outcome_observations | исходы по горизонтам со статусом | (run_id, setup_id, horizon) |
| sim_trades | **симулированные** сделки (не `trades`) | (run_id, trade_no) |
| sim_equity | mark-to-market equity | (run_id, ts_ms) |
| metric_reports | метрики и отчёты по выборкам | report_id |
| research_conflicts | расхождения повторного запуска | id |
Все таблицы неизменяемы (триггеры UPDATE/DELETE).
