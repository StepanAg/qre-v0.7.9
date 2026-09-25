# QRE v0.7.0 — Setup Detection Engine (Phase 6)

> **Setup Detection не является торговым сигналом и не разрешает открытие позиции.** Сетап — это описанная правилами наблюдаемая рыночная ситуация с доказательствами, противоречиями, жизненным циклом и условием невалидности. Фаза не проверяет доходность, win rate или качество стратегий; никаких утверждений об этом не делается.

## Архитектура
```
SQLite candles ─► DataQualityPolicy.prepare/assess (Phase 2)
               ─► для каждого бара t окна воспроизведения:
                    StructureEngine.compute(series.upto(t).tail(N))   (Phase 5, ровно как живой расчёт в момент t)
                    FeatureEngine.evaluate(series.upto(t), atr_14)     (Phase 2, ATR в момент t)
               ─► SetupEngine: один хронологический проход (чистая функция)
               ─► SetupSnapshot (+ RegimeSnapshot as_of как контекст, Phase 3)
               ─► SQLiteSetupStore (0007)
Monitor: Data → Features → Regime → Structure → Setup (отдельные статусы шагов)
```
| Модуль | Роль |
|---|---|
| `app/domain/setup.py` | `SetupTransition`, `Setup`, `SetupSnapshot` + инварианты (state machine, направление, look-ahead) |
| `app/research/setup/config.py` | конфиг, валидация, `config_hash`, `lookback_bars` |
| `app/research/setup/engine.py` | правила всех типов и жизненный цикл |
| `app/research/setup/service.py` | загрузка закрытых свечей, point-in-time структура и ATR, кэш по fingerprint |
| `app/research/setup/version.py` + `VERSION.lock` | контроль версии правил |
| `app/storage/setup.py`, `migrations/0007_setup.sql` | persistence |
| `app/cli/setup_cmds.py` | CLI |
| `config/setup_v1.json` | параметры (`SETUP_CONFIG_FILE`) |

Аддитивные изменения прежних фаз: `ValidatedSeries.tail(n)` (Phase 2, чтобы воспроизвести ровно те данные, что загрузил бы живой расчёт структуры), шаг сетапов в мониторе с отдельным статусом, флаги `--no-setups` у `monitor run` и `monitor supervise`.

## Point-in-time
- Сетап *не читается из БД*: его жизненный цикл на `as_of` — чистая функция закрытых свечей до `as_of`. Повторы, рестарты и ретраи дают тот же результат.
- На каждом баре `j` используются только факты, известные к его закрытию: структура `structure(close_j)` (новые события — только с `event_time == close_j`), ATR на `close_j`. Отбой от диапазона использует структуру, известную **до открытия** бара (`close_{j−1}`).
- Структура на баре t внутри движка побайтово совпадает с живым расчётом `StructureService` в момент t (тест).
- Поля времени: `trigger_time` (факт-триггер стал известен), `setup_time` (кандидат), `confirmed_at`, `closed_at`, `as_of` (горизонт данных), `evaluated_at` (часы стены; **не** часть результата и не участвует в идемпотентности).
- Доменная модель запрещает переходы после `as_of`, неупорядоченные переходы, незаконные переходы state machine.
- Инвариант истории: сетап, видимый на `t`, виден на `t+1` с той же идентичностью, а его переходы только дополняются (тест). Это обеспечивает `lookback_bars = 2·ttl + recent_closed`.

## Жизненный цикл
```
candidate ──confirm──► confirmed ──► invalidated | expired
     └────────────────────────────► invalidated | expired
```
Внутри бара порядок проверок: invalidation → confirmation → expiry. Каждый статус достигается не более одного раза. `expired` — через `ttl_bars` баров после появления кандидата, если сетап не был invalidated (в том числе подтверждённый: его «окно актуальности» закончилось).

## Типы сетапов v1
Все расстояния — в ATR на момент триггера (Phase 2 `atr_14`); нет ATR → триггер пропускается с reason code.

### Breakout (продолжение после BOS)
- Триггер: **только BOS** (не CHoCH, не unclassified — у них нет установленного направления продолжения). Сам BOS даёт лишь кандидата.
- Confirmed: `breakout_hold_bars` закрытий подряд не вернулись за уровень (закрытие ровно на уровне — удержание).
- Invalidated: закрытие обратно за пробитым уровнем (`close < L` для бычьего) — в том числе после подтверждения (несостоявшийся пробой).

### Pullback (возврат к пробитому уровню)
- Взвод от BOS. Сначала цена должна **уйти** от уровня (закрытие дальше `L ± zone`, `zone = pullback_zone_atr·ATR`), затем на **более позднем** баре вернуться в зону, не потеряв её (`low ≤ L+zone`, `close ≥ L−zone`) → кандидат. Без требования ухода бар после BOS «касался» бы уровня автоматически — это был бы шум, а не возврат.
- Приближение к уровню — **только кандидат**. Confirmed: закрытие выше максимума бара касания (возобновление). Invalidated: `close < L − zone`. Взвод отменяется, если уровень потерян до касания или прошло `ttl_bars`.

### Sweep reversal (потенциальный разворот после sweep)
- Триггер: sweep из Phase 5 (фитиль строго за уровнем, закрытие обратно). Buy-side sweep → медвежий кандидат, sell-side → бычий. Sweep сам по себе — только кандидат.
- Confirmed (`sweep_confirmation`): `close_beyond_sweep_bar` — закрытие за противоположным экстремумом бара-свипа; или `opposite_structure_break` — пробой структуры в направлении разворота на этом баре.
- Invalidated: закрытие за экстремумом свипа (цена всё-таки принята за уровнем).

### Range rejection (отбой от границы диапазона)
- Диапазон без look-ahead: ближайший ACTIVE buy-side уровень выше и ACTIVE sell-side уровень ниже предыдущего закрытия, по структуре, известной до открытия бара; ширина `range_min_width_atr..range_max_width_atr`·ATR.
- Кандидат: бар торгуется в зоне касания границы (`range_touch_atr`·ATR), **не пробивая её** (пробой — это уже sweep или invalidation уровня), и закрывается в противоположной части своего диапазона (`rejection_close_fraction`).
- Confirmed: закрытие за противоположным экстремумом бара отбоя. Invalidated: закрытие за границей.
- Дедупликация: пока открыт отбой той же границы в том же направлении, новый не создаётся.
- Ограничение: «диапазон» здесь — пара ближайших активных уровней, а не статистически выделенный range-режим; режим RANGING учитывается только как контекст.

## Контекст, противоречия, режим
- `regime_fit` (allowed / not_allowed / unknown) — соответствие режима на `as_of` списку `allowed_regimes` типа. **Режим не меняет жизненный цикл** (тест): истории режимов нет, поэтому режим — это контекст момента оценки, а не условие перехода.
- `contradictions`: `REGIME_NOT_ALLOWED`, `REGIME_TREND_OPPOSES` и `STRUCTURE_DIRECTION_OPPOSES` (для продолжений), `OPPOSITE_SETUP_ACTIVE` (одновременно открыт сетап противоположного направления). Snapshot-код `CONFLICTING_ACTIVE_SETUPS` — если активны сетапы обоих направлений (например, sweep buy-side и отбой от нижней границы на одном баре).
- Числового score нет: у него не было бы определённого воспроизводимого смысла.

## Data quality
Требуется непрерывный хвост ≥ `structure.min_bars + lookback_bars + 1` закрытых баров (иначе INSUFFICIENT_HISTORY / GAP / STALE, без сетапов). Таймфрейм вне `timeframes` → `TIMEFRAME_NOT_ENABLED`. Направление UNKNOWN → сетап не создаётся.

## Конфигурация (`config/setup_v1.json`) — начальные исследовательские параметры
| Ключ | Значение | Смысл |
|---|---|---|
| enabled_types | все 4 | включённые типы |
| timeframes | 15m, 1h, 4h | где работает движок |
| allowed_regimes | см. файл | для `regime_fit` |
| atr_feature | atr_14 | Phase 2 feature в ценовых единицах |
| ttl_bars | 12 | жизнь сетапа и окно взвода pullback |
| recent_closed_bars | 12 | сколько закрытые сетапы видны в snapshot |
| breakout_hold_bars | 2 | удержания для подтверждения пробоя |
| pullback_zone_atr | 0.5 | зона вокруг пробитого уровня |
| sweep_confirmation | close_beyond_sweep_bar | или opposite_structure_break |
| range_min/max_width_atr | 2 / 12 | допустимая ширина диапазона |
| range_touch_atr | 0.3 | зона касания границы |
| rejection_close_fraction | 0.5 | «закрытие в противоположной половине бара» |
Все ключи обязательны; в `engine.py` нет числовых порогов (тест); версия правил — `VERSION.lock`.

## Persistence (`0007_setup.sql`)
- `setup_snapshots` — неизменяемые; ключ `(series, as_of, engine_version, config_hash, input_fingerprint)`; сравнивается канонический результат **без** `evaluated_at`; тот же ключ с другим результатом → `StorageError`. Ссылки на источники: `structure_input_fingerprint`, `regime_input_fingerprint`.
- `setups` — идентичность (тип, направление, триггер, уровень, время) записывается один раз.
- `setup_events` — журнал жизненного цикла, `UNIQUE(setup_id, status)`, append-only.
- `setup_conflicts` — расхождение идентичности или времени/причины перехода с записанным: первая запись авторитетна, расхождение сохраняется.
- Запись — только на новом закрытом баре (монитор) или по `--save`.

## Monitor
После Structure выполняется шаг Setup со своим статусом (`ok`, `unavailable`, `error`, `persistence_error`, `retry_exhausted`). Ошибка не меняет статусы и snapshots Regime/Structure; повтор на том же баре выполняет **только** шаг Setup (контекст режима берётся из памяти текущего запуска). `monitor health` → `units.*.setup`, блок `setup`. Кэш структуры по fingerprint: на новом баре пересчитывается одна структура, а не всё окно.

## CLI
```
python -m app setup analyze --symbol ETHUSDT --tf 15m [--at ISO] [--save] [--json] [--no-regime]
python -m app setup last    --symbol ETHUSDT --tf 15m [--json]
python -m app setup active  --symbol ETHUSDT --tf 15m
python -m app setup list    --symbol ETHUSDT --tf 15m [--status confirmed] [--type breakout]
python -m app setup events  --setup-id <id>
python -m app setup status
python -m app monitor run|supervise ... [--no-setups]
```

## Известные ограничения
- Режим — только на `as_of`; переходы режимов и hysteresis не учитываются.
- Диапазон определяется ближайшими активными уровнями, а не отдельным статистическим детектором диапазона.
- Фракталы 2/2 (Phase 5) на 15m шумные → много кандидатов; частота на реальных данных не проверена.
- Pullback — только к уровню BOS (не к CHoCH, не к EQ-уровням); sweep — только однобаровый (Phase 5).
- Первый расчёт в мониторе дороже (≈ `lookback_bars` расчётов структуры на единицу), далее — один на бар.
- Реальный прогон на Bybit с шагом Setup не проводился.

## Сознательно отложено
LLM/AI-оценка, Entry Zone, Adaptive Entry Grid, EV, Risk Engine, исполнение, backtest доходности, MTF-комбинирование сетапов, числовой скоринг, сетапы на CHoCH-ретестах и дивергенциях.
