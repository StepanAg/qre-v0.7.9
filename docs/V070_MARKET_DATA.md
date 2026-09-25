# QRE v0.7.0 — Market Data Foundation (Phase 1)

## Поток
```
Bybit public REST ─► UrllibTransport (app/data/http.py, единственный сетевой модуль)
                 ─► RequestExecutor: pacing → запрос → классификация ошибки → retry/backoff
                 ─► BybitRestClient: allow-list эндпоинтов, envelope (retCode/result/time)
                 ─► parser: schema validation → нормализация → domain validation
                 ─► BybitMarketDataProvider: пагинация, dedup, сортировка, unit-sanity
                 ─► BackfillService: clamp к закрытым барам, gap detection, run record
                 ─► SQLiteMarketDataStore (через SerializedWriter) ─► SQLite
Research / Strategy / Backtest ─► MarketDataStore port (get_candles / last_candles), без SQL
```

## Порты (`app/data/ports.py`)
| Порт | Кто реализует | Кто использует |
|---|---|---|
| `MarketDataProvider` | `BybitMarketDataProvider` | `BackfillService` |
| `MarketDataStore` (чтение) | `SQLiteMarketDataStore` | research/strategy/backtest (Phase 2+) |
| `MarketDataSink` (запись) | `SQLiteMarketDataStore` | только `BackfillService` |

Замена источника: любой объект с методами `MarketDataProvider` подставляется в `BackfillService` без изменений (тест `test_provider_is_replaceable`).

## Эндпоинты Bybit v5 (только public, без ключей)
| Эндпоинт | Метод провайдера | Потребитель | Пагинация |
|---|---|---|---|
| `/v5/market/time` | `server_time()` | smoke-тест, контроль рассинхрона часов | — |
| `/v5/market/instruments-info` | `instruments()` | risk/execution (tick, qty step, min notional), время листинга для gap detection | `cursor` |
| `/v5/market/kline` | `candles()` | весь research/backtest | назад по `end`, лимит ≤1000 |
| `/v5/market/tickers` | `tickers()` | будущий сканер/мониторинг; не сохраняется | — |
| `/v5/market/funding/history` | `funding_history()` | учёт funding в backtest | назад по `endTime`, лимит 200 |

Order book не реализован: у него нет потребителя до фазы execution (оценка проскальзывания). Доменная модель `OrderBookSnapshot` осталась.
`BybitRestClient` отклоняет любой путь вне allow-list — клиент физически не может обратиться к `/v5/order/*` или `/v5/account/*`.

**Источник данных — mainnet public API** (`MARKET_DATA_REST_URL=https://api.bybit.com`) независимо от `BYBIT_ENV`. Цены и объёмы testnet синтетические, их нельзя использовать в исследованиях.

## Нормализация и единицы
| Поле | Единица | Bybit поле |
|---|---|---|
| open/high/low/close | quote за 1 base (USDT за BTC) | row[1..4] |
| `volume` | **base coin** (BTC) | row[5] |
| `turnover` | **quote currency** (USDT) | row[6] |
| `Ticker.volume_24h` | base coin | `volume24h` |
| `Ticker.turnover_24h` | quote | `turnover24h` |
| `Ticker.open_interest` | base coin | `openInterest` |
| `FundingRate.rate` | доля за интервал (0.0001 = 0.01 %) | `fundingRate` |

Поля `usd_volume` в системе нет. Для inverse-контрактов единицы другие (volume в USD-контрактах), поэтому категория inverse не поддерживается вовсе.
Проверка единиц (`unit_issue`): `turnover / volume` — это VWAP бара и обязан лежать в `[low, high]` (допуск 0.5 %). Нарушение означает перепутанные единицы → issue `unit_suspect`.

## Время
- Внутри домена — только timezone-aware UTC `datetime`; в SQLite — целые epoch **миллисекунды**.
- `epoch_ms_to_utc` принимает только int/строку из цифр, отвергает float и значения вне 2009–2100. Значение, похожее на секунды, даёт ошибку с подсказкой `looks like epoch SECONDS`.
- Секунды конвертируются только явно (`epoch_seconds_to_utc`); автоопределения «по величине» нет.
- Naive datetime отвергается везде.

## Timeframe
Единственное представление — `Timeframe` (`1m, 5m, 15m, 1h, 4h, 1d`). Коды Bybit (`"15"`, `"60"`, `"D"`) существуют только в `parser.TO_BYBIT_INTERVAL`. `Timeframe.parse("15")` → ошибка. Конфигурация (`TIMEFRAMES=15m,1h`) валидируется тем же `Timeframe.parse`. Бары выровнены по эпохе UTC (1d начинается в 00:00 UTC, 4h — в 0/4/8/… UTC).

## Закрытая и открытая свеча
- `Candle.is_closed = open_time + timeframe <= server_time`, где server_time берётся из поля `time` ответа Bybit (не из локальных часов).
- `BackfillService` дополнительно обрезает диапазон до `floor(now)`, а Sink отказывается сохранять `is_closed=False` (`StorageError`).
- Таблица `candles` содержит **только закрытые** бары и защищена триггером от UPDATE: закрытая свеча неизменна.

## Backfill
1. Выравнивание диапазона `[ceil(start), min(ceil(end), floor(now)))`.
2. Пагинация назад: `end = oldest - 1 ms` до достижения `start`, пустой страницы или неполной страницы. Защита: бюджет страниц `ceil(bars/limit)+2` и проверка прогресса → `PaginationError`.
3. Dedup: идентичные повторы схлопываются; конфликтующие (тот же ключ, разные значения) **отбрасываются оба** и дают issue → дырка видна gap detection.
4. Запись: новые — INSERT; идентичные — `unchanged`; отличающиеся от сохранённых — `stored_conflict`, сохранённое не перезаписывается.
5. Gap detection по сохранённым данным в `[max(start, launch_time), end)`.
6. Запись прогона в `backfill_runs` + `data_quality_issues`. Статус: `ok` / `ok_with_gaps` / `completed_with_issues` / `failed` (исключение пробрасывается дальше).

## Warmup
`plan_history(tf, usable_bars, bars_per_value, end)`: `total = usable + bars_per_value − 1`, `warmup = bars_per_value − 1`.
**Изменено в Phase 2:** data layer больше не содержит методологии индикаторов. `bars_per_value` берётся из требований features (`FeatureService.bars_per_value(names)`; для EMA-200 = 801 = 200 seed + 600 рекурсивный прогрев + 1 бар для детекции STALE). Прежнее правило `warmup = 3 × lookback` давало 600 + usable баров, а движку для одного значения EMA-200 нужно 800 непрерывных — backfill отчитывался «ok», но EMA-200 была недоступна во всех точках.
- без индикаторов, 10 баров → 10;
- EMA-200, 96 баров → 96 + 600 = 696.

`ensure_history()` загружает историю и **бросает `InsufficientHistory`**, если фактически доступно меньше `total_bars` (например, инструмент листингован недавно или внутри окна есть дыры). Индикатор не будет посчитан на неполных данных.

## Retry, rate limit, pacing
Всё в `RequestExecutor`; в бизнес-коде `sleep()` нет.
| Ситуация | Повтор | Итог |
|---|---|---|
| timeout, connection error | да | `RetriesExhausted(last=…)` |
| HTTP 5xx | да | то же |
| HTTP 429 | да, ждём `X-Bapi-Limit-Reset-Timestamp` (≤30 с) | то же |
| HTTP 403 | да (IP rate limit), **но может быть гео-блок** — текст ошибки об этом говорит | то же |
| retCode 10006 / 10016 / 10429 | да | то же |
| HTTP 4xx, retCode 10001 и прочие | нет | исходная ошибка |
| невалидный JSON / схема | нет | `MalformedResponse` |

Backoff: `0.5·2^(n-1)` с ±20 % jitter, потолок 8 с, по умолчанию 4 попытки. Pacer: минимум `HTTP_MIN_INTERVAL_MS` (100 мс) между запросами — на порядок ниже лимита Bybit (600 запросов / 5 с на IP).

## Single writer
`SerializedWriter`: одно соединение, один lock, транзакции `BEGIN IMMEDIATE`. Все записи market data идут через него (проверено тестом с 8 потоками). Сейчас ingestion однопоточный, поэтому lock достаточно. Для WS (будущая фаза) реализацию можно заменить на поток-писатель с очередью; API вызывающих не изменится.

## CLI
```
python -m app data instruments [--symbols BTCUSDT ETHUSDT]
python -m app data backfill --symbols BTCUSDT --tf 15m --days 30
python -m app data backfill --tf 1h --bars 100 --for-features ema_200 atr_14
python -m app data funding --days 30
python -m app data status | gaps --symbol BTCUSDT --tf 15m | smoke
```
Каждая команда печатает технические метрики (`IngestionMetrics`), кумулятивные за процесс.
