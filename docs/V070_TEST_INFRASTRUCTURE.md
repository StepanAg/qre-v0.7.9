# QRE v0.7.0 — Test Infrastructure

## Запуск
```
python test.py                     # таблица статусов
python test.py --json              # JSON в stdout (для CI/автоматизации)
python test.py --json-file r.json  # плюс файл
python test.py --only "Domain models" --only CLI -v
python -m app test                 # то же через CLI
```
Требования: Python ≥ 3.11, без внешних зависимостей, без сети и реальных ключей. Временные БД создаются в `tempfile` и удаляются.

## Статусы
| Статус | Значение |
|---|---|
| PASS | тесты реально выполнены (≥1) и все прошли |
| FAIL | хотя бы один тест упал, модуль не импортируется, тестов 0, или зарегистрированная реализация отсутствует |
| PARTIAL | часть тестов пропущена, или реализация есть, а тестов нет |
| NOT_IMPLEMENTED | компонент не зарегистрирован в `app/core/registry.py` — ожидаемо для будущей фазы, не ошибка |
| SKIPPED | проверка неприменима (например, нет `.env`) |
| WARNING | не блокирует, но требует внимания (например, «тонкая» документация) |

Итог: `FAIL`, если есть любой FAIL; `PARTIAL`, если есть PARTIAL/WARNING; иначе `PASS`.
Exit code = 1 только при FAIL в **критичной** категории.

## Защита от фиктивного PASS
1. Ноль собранных тестов = FAIL.
2. Категория «Test infrastructure» прогоняет через раннер синтетические наборы (падающий, проходящий, пропущенный, пустой) и проверяет классификацию.
3. Будущие компоненты берутся из реестра: пока `implementation=None` → `NOT_IMPLEMENTED`. Зарегистрировал класс без тестов → `PARTIAL`. Класс не импортируется → `FAIL`.

## Категории Phase 0
Python environment · Imports (все модули `app.*`) · Versioning · Configuration · Domain models · Accounting invariants · Database bootstrap (включая restart recovery) · Architecture rules (AST) · Test infrastructure · Safety configuration · Logging · CLI · Documentation (некритичная) · Local .env (некритичная) · Market Data / Research / Strategy / Risk / Backtest / Execution / Reconciliation / AI → NOT_IMPLEMENTED.

## Как добавить категорию в следующей фазе
1. Написать `tests/test_<x>.py` (unittest).
2. Реализовать компонент и прописать в `app/core/registry.py`: `implementation="app.data.bybit_rest:BybitMarketData"`, `test_module="tests.test_market_data"`.
3. Раннер автоматически переведёт категорию из NOT_IMPLEMENTED в PASS/FAIL. Править `test.py` не нужно.

Сетевые (интеграционные) тесты в будущем помечаются `@unittest.skipUnless(os.getenv("QRE_ONLINE_TESTS"))` и дают SKIPPED в обычном прогоне.

## JSON-формат
```json
{"project":"qre","version":"0.7.0","phase":0,"overall":"PASS","critical_fail":false,
 "duration_s":0.3,"counts":{"PASS":13,"FAIL":0,...},
 "results":[{"category":"Imports","status":"PASS","detail":"42 modules","tests_run":42,
             "failures":[],"duration_s":0.08,"critical":true}]}
```

## Phase 1: offline baseline и сетевой режим
- `python test.py` устанавливает **network guard** (`tests/netguard.py`): `socket.connect`, `connect_ex`, `create_connection` и `getaddrinfo` бросают `NetworkBlocked`. Любой случайный сетевой вызов в любом тесте падает сразу. Тест `test_offline_baseline_active_under_runner` проверяет, что guard действительно активен.
- `python test.py --network` дополнительно запускает `tests/test_md_network.py` (реальный Bybit public API: server time, instrument info, kline). Guard снимается только на время этой категории. Без флага категория `Bybit Network Smoke` = SKIPPED и не влияет на итог; с флагом её FAIL критичен.
- Категории Phase 1: Market Data Imports, Provider Interface, Bybit Response Parsing, Normalization, Timestamp Validation, Candle Invariants, Fixtures, Failure Handling, Rate Limits & Retry, Duplicate Detection, Gap Detection, Pagination, Backfill, Idempotency, Warmup, SQLite Persistence, Migration, Single Writer, Network Isolation, Architecture Rules. Категория может указывать на класс: `tests.test_md_backfill.WarmupTests`.
- Fixtures: `tests/fixtures/bybit/*.json` (формат Bybit v5). Многостраничные сценарии — `tests/fakes.SyntheticBybit`.
- `scripts/offline_ingest_demo.py` — полный прогон ingestion на синтетической бирже с печатью метрик.

## Phase 2
Новые категории: Data Quality Policy, Gap Classification, No Silent Fill, Insufficient History, Closed Candle Protection, Feature Registry, Feature Catalog Sync, Feature Validation, CVD Naming, EMA, SMA, ATR, Volatility, Returns, Volume Features, NaN/Inf Protection, Feature Versioning, Feature Determinism, Look-ahead Protection, Multi-Timeframe Alignment, Feature Persistence, Feature Cache, Research Safety, Data Quality Metrics.
- `Result.notes` печатается всегда (используется блоком Data Quality Metrics для coverage с объяснением исключений).
- `VERSIONS.lock` и сгенерированный каталог проверяются тестами. После намеренного изменения: сначала новая версия feature, затем `python scripts/lock_feature_versions.py` и `python scripts/gen_feature_catalog.py`.
- Тесты миграций Phase 1 переписаны на проверку порядка и префикса: раньше они требовали ровно две миграции и ломались бы от каждой новой.

## Phase 3
Категории: Regime Classification, Regime Data Quality, Regime Config, Regime Scenarios, Regime Data Failures, Regime MTF, Regime Look-ahead, BTC Context, Regime Determinism, Regime Persistence, Regime CLI, Regime Safety. Сценарии (`tests/regime_data.py`) строятся из детерминированных рядов up / down / range / shock / turn_down и прогоняются через настоящие Feature Engine и SQLite.

## Phase 4
Категории: Monitor Cycle, Monitor Data Conditions, Monitor Retry & Recovery, Monitor Idempotency, Monitor Lifecycle, Monitor CLI, Monitor Config, Monitor Safety. Обвязка `tests/monitor_harness.py`: `ClockedBybit` (синтетический public API, чьё «сейчас» идёт по фейковым часам; листинги, пропуски, неизвестные символы, сломанные TF, очередь сбоев) и `FakeTime` (waiter сдвигает часы вместо сна). Час опроса проверяется за доли секунды без сети.

## Phase 5
Категории: Swing Detection, BOS Detection, CHoCH Detection, EQH / EQL, Liquidity Levels, Sweep Detection, Structure No-Look-Ahead, Structure Closed Candles, Structure Gap Handling, Structure Data Quality, Structure Determinism, Structure Persistence, Structure Config, Structure MTF Alignment, Structure Safety, Structure Monitor Integration, Structure CLI, Structure Offline Baseline.
Фикстуры `tests/structure_data.py`: вручную построенные пути (`PATH_A` — swings/BOS/CHoCH, `PATH_B` — EQH/EQL, ложный пробой фитилём, sweep, касание) с ожидаемыми событиями, выведенными вручную; `mirror()` для проверки зависимости меток от направления; seeded random walk для baseline. Offline baseline считает всё в памяти и явно помечает результаты как SYNTHETIC.

## Phase 6
Категории: Setup Breakout, Setup Pullback, Setup Sweep Reversal, Setup Range Rejection, Setup Direction/Data/Regime, Setup Point-in-Time, Setup Determinism, Setup Config, Setup Persistence, Setup Safety, Setup + Regime/Structure, Setup Monitor Integration, Setup CLI. Фикстуры `tests/setup_data.py` (пути PATH_P/N/S1/S2/R поверх PATH_A/PATH_B) с жизненными циклами, выведенными вручную (docstring `tests/test_setup.py`).
Замечание о счётчике: раннер запускает модули и как категории, и как компоненты — сумма `tests_run` по категориям завышена; число уникальных тестов даёт `unittest discover`.

## Phase 7
Категории: Research Dataset, Research Replay, Setup Outcomes, Backtest Engine, Time Splits, Research Persistence, Research Safety, Research CLI, Research + Regime; компонент Backtest. Фикстуры `tests/research_data.py`: `PATH_BT` с вручную выведенными MFE/MAE/forward return и расчётом сделки (docstring `tests/test_research.py`). Число уникальных тестов — через `unittest discover` (раннер считает модули компонентов повторно).

## Phase 8
Категории: Analytics Read-only Foundation, Distributions, Performance/Risk/Costs, Drawdown, Excursions/Exits, Groups/Portfolio/Periods, Funnels/Untraded, Funnel on Real Run, Versions/Compare, Data Quality, CLI; компонент Analytics (`AnalyticsSmokeTests`). Фикстуры `tests/analytics_data.py` пишут прогоны через настоящий `SQLiteResearchStore.save_run` без replay; ожидаемые числа выведены вручную в docstring. Read-only проверяется контрольными суммами всех таблиц и хэшем файла БД вокруг каждой CLI-команды. Число уникальных тестов — только через `unittest discover`.

## Phase 9
Категории: Phase 9 Contracts, Strategy v1, Risk Engine v1, Decision Backtest Loop, Equivalence with Phase 7, Phase 7 Compatibility, Strategy+Risk Pipeline, Strategy+Risk Analytics, Strategy+Risk CLI, Legacy Contract Retirement; компоненты Strategy и Risk Engine. `Phase7CompatibilityTests` фиксирует идентификаторы Phase 7 (dataset, replay, backtest run id и result hash), записанные до изменений Phase 9. Число уникальных тестов — только `unittest discover`.
