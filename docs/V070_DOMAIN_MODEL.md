# QRE v0.7.0 — Domain Model

Все сущности — `frozen` dataclasses. Денежные величины — `Decimal`; `float` отвергается конструктором (`DomainError`). Время — только timezone-aware UTC.

## Цепочка

```
Signal ─► Setup ─► EntryPlan ─┐
                   RiskPlan ──┴► Order ─► Fill(s) ─► PositionEvent(s) ─► Trade ─► PnLBreakdown / R
```

## Market Data (`domain/market.py`)
- **Symbol** (name, category=linear|spot).
- **InstrumentInfo** — tick size, qty step, min qty, min notional (может быть None), base/quote/settle coin, status, contract type, price scale, max qty, leverage filter, funding interval, launch time. Отсутствующие в API поля — None, не выдумываются.
- **Timeframe** (Phase 1) — `1m,5m,15m,1h,4h,1d`, единственное представление таймфрейма; `floor/ceil/is_aligned`, `ms`.
- **Candle** — `timeframe: Timeframe`, `open_time` выровнен; OHLC-инварианты; `volume` = base coin, `turnover` = quote; `close_time` (исключающая граница); флаг `is_closed`.
- **Ticker** (bid/ask опциональны; volume_24h base, turnover_24h quote, open interest base), **OrderBookSnapshot** (запрет «перекрещенной» книги), **FundingRate**.

## Research (`domain/research.py`)
- **Feature** (Phase 2) — `value: float | None` + `status: FeatureStatus` (VALUE/NOT_AVAILABLE/INSUFFICIENT_HISTORY/DATA_QUALITY_FAILURE/INVALID), `version`, `lookback`, `min_observations`, `bars_used`, `unit`, `quality`, `gap_severity`, `reason`. Значение есть только у VALUE; NaN/inf запрещены; у не-VALUE обязательна причина.
- **FeatureSnapshot** — symbol, as_of, timeframe, features, `feature_set_hash`, `input_fingerprint`, `engine_version`; `get(name)` строгий: неизвестное имя → `KeyError`, не-VALUE → `DomainError`.
- **MarketState** (regime, horizon, confidence), **ResearchSnapshot** — единственный вход стратегии и AI.

## Decision (`domain/decision.py`)
- **Signal** — мнение о рынке (strategy_id+version, strength). Не ордер.
- **Setup** — сигнал, прошедший фильтры, со сроком жизни.
- **EntryPlan** — зона входа, тип ордера, TIF, `expires_at` (дедлайн maker-ожидания — не блокирующий sleep), `fallback_to_market`, `max_slippage_bps`.
- **RiskPlan** — `initial_stop`, `planned_entry`, `planned_qty`. Заморожен; проверка стороны стопа.

## Execution (`domain/execution.py`)
- **Order** — `local_order_id`, `client_order_id` (≤36, orderLinkId), `exchange_order_id`, `status`, `source`, `reconciliation_status`. Переходы — через `transition()` по явной таблице; терминальные статусы не меняются. Статус `UNKNOWN` — для потерянного ack/таймаута, выход из него только через reconciliation.
- **OrderEvent** — история ордера, дедуп по `idempotency_key`.
- **Fill** — факт исполнения: `exec_id` (ключ дедупликации), qty, price, fee (+ = расход, − = ребейт), `is_maker`, `expected_price` для атрибуции проскальзывания. `local_order_id=None` — исполнение ордера, которого мы не создавали (обнаружено при сверке).
- `filled_qty(order, fills)` — исполненный объём выводится из fills, счётчика нет.
- **Position** — снимок экспозиции с `source` (локальный или биржевой — никогда не сливаются).
- **PositionEvent** — open/increase/reduce/close, ссылка на `exec_id`.

## Accounting (`domain/accounting.py`)
- **Trade** — эпизод позиции от первого входа до нулевого остатка. Хранит `legs` (TradeLeg из fills) и `funding`. Всё остальное вычисляется replay'ем по `exec_time`.
- Инварианты: повторный `exec_id` — no-op; выход больше открытого объёма → `InvariantViolation` (защита от phantom/duplicate close); fill после закрытия → ошибка; сторона fill должна соответствовать роли; `exit_reason` задаётся один раз и только для закрытой сделки.
- **FundingEvent** — знаковая сумма (+ получено), дедуп по `exchange_ref`.
- **PnLBreakdown**:
  ```
  theoretical_gross − slippage_cost = gross   (gross — по фактическим ценам)
  gross − fees + funding             = net
  ```
  Проскальзывание уже внутри `gross`, поэтому показывается для атрибуции и **не вычитается повторно**.
- Средняя цена входа — средневзвешенная, пересчитывается при доливках; частичные выходы реализуют PnL относительно текущей средней.

## Методология R
- `initial_risk_amount = Σ |entry_price_i − initial_stop| × qty_i` по всем ENTRY-ногам.
- `realized_R = net_pnl / initial_risk_amount`, только для закрытой сделки; при нулевом риске — `None`.
- `initial_stop` заморожен в RiskPlan и защищён триггером в БД. Перенос стопа (BE, трейлинг) — отдельные события и **не меняют** R-знаменатель.
- При частичном входе риск считается по фактически исполненному объёму (а не по плановому). Плановый риск доступен как `risk_plan.planned_risk_amount` для анализа недоисполнения.

## Account & Analytics
- **AccountSnapshot** — equity/wallet/available/unrealized с `source`.
- **TradeMetrics / Drawdown / ExecutionMetrics / PerformanceSnapshot**. `trade_metrics()` учитывает только CLOSED сделки.

## Хранение
Таблицы: `signals, trades, orders, order_events, fills, trade_legs, funding_events, position_events, exchange_snapshots, account_snapshots, reconciliation_runs, event_log, schema_migrations`. Decimal хранится как TEXT.

## Regime (Phase 3, `domain/regime.py`)
- **TimeframeRegime** — режим одного таймфрейма: regime, trend_direction, trend_strength (категориальный), volatility_state, data_quality, data_through, reason_codes, metrics (нормированные входы). Инварианты: UNKNOWN обязан иметь код `UNKNOWN:<tf>:<why>`; режим, отличный от UNKNOWN, возможен только при VALID-данных.
- **BtcContext** — AVAILABLE / SELF / STALE / UNAVAILABLE; недоступный контекст не может нести режим.
- **RegimeSnapshot** — итог на `as_of`; верхний уровень обязан совпадать с базовым таймфреймом. Канонический JSON, точный round-trip. Поля confidence нет.

## Structure (Phase 5, `domain/structure.py`)
- **Swing** — pivot_time (бар экстремума) и confirmed_at (закрытие бара подтверждения); confirmed_at > pivot_time.
- **StructureEvent** — BOS / CHoCH / break_unclassified; инварианты: BOS ⇔ prior == direction, CHoCH ⇔ prior известен и противоположен, unclassified ⇔ prior UNKNOWN; уровень подтверждён до открытия бара пробоя; только VALID-данные.
- **LiquidityLevel** — сторона, источник, цена, зона, исходные экстремумы, created/updated, статус, допуск и ATR (для EQ). Это гипотеза, не наблюдение стакана.
- **SweepEvent** — уровень, бар-свип, экстремум, закрытие, метод.
- **StructureSnapshot** — всё на `as_of`; запрещает любые факты позже `as_of` и любую структуру на невалидных данных. Поля confidence нет.

## Setup (Phase 6, `domain/setup.py`)
- **SetupTransition** — статус, время закрытия бара, причина.
- **Setup** — тип, направление (только bullish/bearish), триггер и время его известности, ключевой уровень, условие невалидности, переходы (state machine candidate → confirmed → invalidated/expired), доказательства, противоречия, `regime_fit`, `data_quality`, версия и config hash. Статус, подтверждённость, качество данных и соответствие режиму — разные поля; поля «торговать можно» нет.
- **SetupSnapshot** — сетапы на `as_of`, ссылки на контексты Regime/Structure; `evaluated_at` не входит в канонический результат.
