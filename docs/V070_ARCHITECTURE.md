# QRE v0.7.0 — Архитектура

Новый проект, написан с нуля. Старый код не используется ни как runtime-зависимость, ни как шаблон; из него взяты только выводы (см. `V070_LESSONS_LEARNED.md`). Правило AST-теста `test_no_old_project_imports` это фиксирует.

## Поток

```
Data → Research → Decision → Execution → Accounting → Analytics
```

## Структура

| Пакет | Ответственность | Может зависеть от |
|---|---|---|
| `app/core` | id, idempotency keys, часы (UTC), ошибки, логирование, реестр компонентов | только stdlib |
| `app/config` | единственное место чтения env; типизированные `Settings`, валидация безопасности | core, domain (value objects: `Timeframe`) |
| `app/domain` | чистые сущности и инварианты (Decimal, frozen dataclasses) | только stdlib и domain |
| `app/storage` | SQLite, миграции, репозитории, `SerializedWriter`; **единственный** слой с SQL | core, domain (не data) |
| `app/data` | порты `MarketDataProvider/Store/Sink`, Bybit public REST адаптер, качество данных, backfill. Сеть — **только** `app/data/http.py` | domain, core, config |
| `app/research` | Data Quality Policy, Feature Engine, реестр features, MTF alignment, FeatureService | domain, core, data.ports, data.quality |
| `app/strategy` | порт `Strategy`: Signal → Setup → EntryPlan. Не считает размер, комиссии, не знает биржу | domain, research |
| `app/risk` | порт `RiskEngine`: EntryPlan → RiskPlan или отказ | domain |
| `app/execution` | порты `ExecutionGateway`, `ExchangeStateReader`, **safety gate** | domain, config, core |
| `app/accounting` | сборка Trade из сохранённых fills/funding (replay) | domain |
| `app/analytics` | метрики по закрытым сделкам | domain, accounting |
| `app/ai` | порт `LLMClient.decide(AIContext) -> AIDecision` | только domain/stdlib |
| `app/cli` | тонкий CLI, без бизнес-логики | всё |

Правила зависимостей проверяются автоматически (`tests/test_architecture.py`). Пустых папок нет: у каждого пакета есть docstring с его ответственностью, это тоже проверяется.

**Отличия от предложенной в ТЗ структуры.** Добавлены `app/storage` (SQL изолирован в одном месте — в старом проекте API-клиент и логика писали в БД напрямую) и `app/ai` (порт AI отдельно от стратегии, чтобы AI никогда не импортировал storage/execution). `app/research` и `app/risk` пока содержат только порты — это интерфейсы, а не заглушки-реализации, и тест-раннер помечает их `NOT_IMPLEMENTED`.

## Ключевые решения

1. **Signal ≠ Order ≠ Fill ≠ Position ≠ Trade** — разные типы без наследования (тест проверяет).
2. **Fill — факт, PnL — производная.** Колонки PnL в БД нет. Trade пересобирается из `trade_legs` + `funding_events`.
3. **Идемпотентность на уровне БД:** `fills.exec_id` PK, `trade_legs.exec_id` UNIQUE, `funding_events.exchange_ref` UNIQUE, `event_log.idempotency_key` UNIQUE, `orders.client_order_id` UNIQUE.
4. **Append-only факты:** триггеры запрещают UPDATE/DELETE по `fills`; `trades.initial_stop` неизменяем (триггер).
5. **Local ≠ Exchange state:** у Order/Fill/Position есть `source` и `reconciliation_status`; сырые снимки биржи — в `exchange_snapshots`, отдельно от локальных таблиц.
6. **Restart recovery:** всё критичное — в SQLite (WAL, `synchronous=FULL`). `client_order_id` генерируется и сохраняется **до** отправки ордера, поэтому после рестарта ордер можно найти на бирже, даже если ack потерян (статус `UNKNOWN`).
7. **Safety:** три уровня — дефолты `false`; валидация при старте (`SafetyViolation`); вшитый потолок `BUILD_EXECUTION_CEILING = PAPER`. Единственный gateway в Phase 0 — `DisabledGateway`. В коде нет эндпоинтов создания ордеров (тест).
8. **AI provider-agnostic:** AI получает `AIContext` (подготовленные данные, без секретов) и возвращает структурированное `AIDecision`. Действия, повышающие риск, помечены `requires_human`.

## Процесс рестарта (целевой, Phase 5)

```
start → load Settings → bootstrap DB (migrate)
      → OrderRepository.non_terminal()           # CREATED/SUBMITTING/ACCEPTED/PARTIAL/UNKNOWN
      → ExchangeStateReader: open orders, executions_since(last_fill_time - margin), positions
      → reconcile: вставка недостающих fills (idempotent), обновление статусов,
        EXCHANGE_ONLY / LOCAL_ONLY → алерт, без автоматического «придумывания» сделок
      → rebuild trades → только после этого разрешён приём сигналов
```

## Запуск

```
python -m app version | config | status [--json] | test | db-init
python test.py [--json] [--json-file r.json] [--only CATEGORY] [-v]
```

## Изменения Phase 1
- Сетевые библиотеки разрешены только в `app/data/http.py`; модули `app.data.http` и `app.data.bybit` могут импортировать только `app/data` и `app/cli` (AST-тесты `test_network_libs_only_in_http_module`, `test_network_layer_not_imported_by_core_layers`, самопроверка `test_rule_is_real`).
- `config → domain` разрешено ради единого `Timeframe` (domain по-прежнему ничего не импортирует).
- Подробности: `V070_MARKET_DATA.md`, `V070_MARKET_DATA_SCHEMA.md`, `V070_DATA_QUALITY.md`.

## Изменения Phase 2
- `app/research` реализован: `DataQualityPolicy` → `FeatureEngine` → `FeatureSnapshot`. Research читает данные только через порт `MarketDataStore`; сохранение снапшотов — через порт `FeatureSnapshotStore` (реализация `app/storage/features.py`).
- Миграция `0003_features.sql` (отдельные таблицы, не смешаны с market data).
- `Feature.value` теперь `float` + обязательный `FeatureStatus` (см. `V070_FEATURE_METHODOLOGY.md`).
- Подробности: `V070_DATA_QUALITY_POLICY.md`, `V070_FEATURE_ENGINE.md`, `V070_FEATURE_CATALOG.md`.

## Изменения Phase 3
- Добавлен `app/research/regime/` (Regime Engine) внутри research-слоя, с теми же ограничениями импорта (только domain/core/data.ports/data.quality).
- Пороги режима — в `config/regime_v1.json`; путь — `REGIME_CONFIG_FILE`, загрузка — в CLI (composition root).
- Миграция `0004_regime.sql`: таблица `regime_snapshots` (append-only).
- Заглушка Phase 0 `MarketState` (с неопределённым `confidence: Decimal`) заменена на `RegimeSnapshot`; `ResearchSnapshot.state` → `ResearchSnapshot.regime`. `MarketRegime` приведён к классам ТЗ Phase 3 (+TRANSITION).
- В реестре компонентов Strategy и Risk Engine перенесены на phase 4, так как phase 3 теперь — Regime Engine.

## Изменения Phase 4
- Новый слой `app/monitor/` (runtime): использует `BackfillService`, `MarketDataStore`, `RegimeService` и порт `MonitorRunStore`. AST-правило запрещает ему execution/strategy/risk/ai/accounting/analytics/cli/storage/config и сетевой слой.
- `RegimeService.analyze()` разделён на публичные шаги `compute_features()` и `classify()` без изменения поведения (хэш правил режима не изменился), чтобы runtime различал ошибки Feature Engine и Regime Engine.
- Миграция `0005_monitor.sql`; хранилище `app/storage/monitor.py`; CLI `app/cli/monitor_cmds.py`; конфиг `config/monitor_v1.json` (`MONITOR_CONFIG_FILE`).
- `core/logging.add_file_handler()` — лог монитора дублируется в файл.
- В реестре Strategy/Risk/Backtest перенесены на phase 5.

## Изменения Phase 5
- `app/research/structure/` — Market Structure & Liquidity Engine в research-слое (те же ограничения импорта: domain/core/research/data.ports/data.quality). Переиспользует `DataQualityPolicy` и `FeatureEngine` (ATR point-in-time) без изменений.
- `app/domain/structure.py`, структурные enums в `domain/enums.py`.
- Миграция `0006_structure.sql`, `app/storage/structure.py`, CLI `app/cli/structure_cmds.py`, `config/structure_v1.json` (`STRUCTURE_CONFIG_FILE`).
- Monitor: необязательный шаг структуры после Regime с отдельным статусом (`StructureStatus`); `build_monitor(..., structure=True)`, `monitor run --no-structure`. Интерфейсы Phase 0–4 не менялись; `monitor last` дополнен строкой о структуре.
- В реестре Strategy/Risk/Backtest перенесены на phase 6, Execution/Reconciliation — на phase 7.

## Изменения Phase 6
- `app/research/setup/` — Setup Detection Engine (research-слой, те же правила импорта). Переиспользует Structure (на каждом баре, point-in-time), Feature Engine (ATR), Regime (контекст).
- `app/domain/setup.py`, enums `SetupType`, `SetupStatus`, `RegimeFit`.
- `ValidatedSeries.tail(n)` — аддитивный метод Phase 2.
- Миграция `0007_setup.sql`, `app/storage/setup.py`, CLI `app/cli/setup_cmds.py`, `config/setup_v1.json` (`SETUP_CONFIG_FILE`).
- Monitor: шаг Setup после Structure (`SetupStepStatus`), `--no-setups` у `monitor run` и `monitor supervise`.
- Реестр компонентов: Setup Detection — phase 6; Strategy/Risk/Backtest — 7; Execution/Reconciliation/AI — 8.

## Реестр модулей (актуальный)
| Слой | Модули |
|---|---|
| core | ids, clock, errors, logging (ротация), registry |
| config | settings (+ *_CONFIG_FILE) |
| domain | market, research, regime, structure, setup, decision, execution, accounting, account, analytics |
| data | ports, http (единственный сетевой модуль), bybit/*, quality, backfill, metrics |
| research | quality, engine, service, features/*, regime/*, structure/*, setup/*, alignment (только тесты — см. аудит) |
| storage | database, writer, market_data, features, regime, structure, setup, monitor, repositories |
| monitor | config, health, runtime, ports |
| cli | main, data, features, regime, structure, setup, monitor (+ supervise), supervisor, keepawake |

## Изменения Phase 7
- `app/research/lab/` — research & backtest (research-слой, те же правила импорта; без SQL, сети, execution, LLM — тесты).
- `app/domain/research_lab.py`, enums `OutcomeStatus`, `SimExitReason`.
- Миграция `0008_research.sql`, `app/storage/research.py`, CLI `app/cli/research_cmds.py`, `config/research_v1.json`, `config/backtest_v1.json` (`RESEARCH_CONFIG_FILE`, `BACKTEST_CONFIG_FILE`).
- Backtest считает PnL через учёт Phase 0 (`Trade`/`Fill` с `EventSource.SIMULATOR`), симулированные сделки хранятся только в `sim_trades`.
- Monitor research не запускает; реестр: Backtest — реализован (phase 7); Strategy/Risk — 8; Execution/Reconciliation/AI — 9.
- Реестр модулей дополнен: research/lab (config, versions, dataset, replay, outcomes, backtest, metrics, stats, splits, pipeline); storage/research; cli/research_cmds.

## Изменения Phase 8
- `app/analytics/` наполнен: ports, sources, filters, distributions, drawdown, excursions, engine (`AnalyticsEngine`), quality, report. Читает только через `app/storage/analytics_read.py` (своё соединение `mode=ro`, только SELECT — AST-тест); analytics не импортирует storage и sqlite3 (AST-тест).
- `app/research/lab/stats.py`: добавлены `percentile`, `sample_warning` (прежние функции не менялись). `lab/metrics.py`, `analytics/metrics.py`, `domain/analytics.py` — без изменений.
- CLI `python -m app analytics ...` (`app/cli/analytics_cmds.py`). Таблиц и миграций нет (решения D1 = нет, D3 = нет).
- Реестр: `Analytics`, phase 8; `CURRENT_PHASE = 8`, версия 0.7.0 без изменений.

## Изменения Phase 9
- `app/strategy/` (StrategyV1, `StrategyRiskGate`) и `app/risk/` (RiskEngineV1, первый потребитель `RiskSettings`): чистые детерминированные модули; AST-правило — без часов, случайности, сети, storage, LLM, исполнения.
- Контракты Phase 9 — в `app/domain/decision.py` (`SetupView`, `StrategyDecision`, `RiskDecision`, `PortfolioState`, `InstrumentRules`, `GateResult`); `RiskPlan` не изменён; `Signal`, `decision.Setup`, `ResearchSnapshot` — deprecated (сохранены).
- Backtest получает решения через порт `research/lab/ports.DecisionGate`; `DecisionBacktestEngine` — подкласс, путь v1 не менялся. Прогоны Strategy + Risk — `kind = backtest` с `config.pipeline = strategy_risk_v1`; решения — в `metric_reports`. Миграций нет.
- Analytics: воронка для таких прогонов со стадиями `strategy_accepted` и `risk_approved`.
- CLI: `research decide`, `research decisions`. Реестр: Strategy и Risk Engine — phase 9; `CURRENT_PHASE = 9`.
