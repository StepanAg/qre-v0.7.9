# QRE v0.7.0 — Market Structure & Liquidity Engine (Phase 5)

Аналитический движок: swings, BOS, CHoCH, equal highs/lows, потенциальные уровни ликвидности и их sweeps. Всё выводится **только из закрытых OHLCV-свечей**. Сигналов, размера позиции, ордеров и LLM нет.

> **Важно.** Liquidity levels здесь — *гипотезы* о зонах, где могут находиться стоп-ордера и отложенные ордера, выведенные из формы цены. Это **не** наблюдение биржевого стакана: лимитные ордера, стены L2 и микроструктура из свечей не восстанавливаются и не симулируются.

## Архитектура
```
SQLite candles ─► StructureService ─► DataQualityPolicy.prepare/assess (Phase 2, без изменений)
                                   ─► StructureEngine (чистая функция, один символ, один TF)
                                        ATR для EQH/EQL: FeatureEngine.evaluate(series.upto(t), atr_14) — Phase 2
                                   ─► StructureSnapshot ─► SQLiteStructureStore (0006: snapshots + event log)
Market Monitor (Phase 4): … → Regime → [сохранён] → Structure (отдельный шаг, свой статус)
```
| Модуль | Роль |
|---|---|
| `app/domain/structure.py` | Swing, StructureEvent, LiquidityLevel, SweepEvent, StructureSnapshot (+ инварианты look-ahead) |
| `app/research/structure/config.py` | конфиг, валидация, `config_hash` |
| `app/research/structure/engine.py` | алгоритмы (state machine по барам) |
| `app/research/structure/service.py` | загрузка закрытых свечей, point-in-time ATR |
| `app/research/structure/version.py` + `VERSION.lock` | контроль версии правил |
| `app/storage/structure.py`, `migrations/0006_structure.sql` | persistence |
| `app/cli/structure_cmds.py` | CLI |
| `config/structure_v1.json` | все параметры (`STRUCTURE_CONFIG_FILE`) |

## Семантика времени
- `pivot_time` — время открытия бара-экстремума.
- `confirmed_at` — время **закрытия** бара `pivot + right`; с этого момента swing известен системе.
- Уровень может быть пробит/свипнут только баром, который **открылся** после `confirmed_at` / `created_at`. Доменная модель запрещает иное (`StructureEvent` с `swing_confirmed_at > break_bar_time` не создаётся).
- `event_time` = время закрытия бара события. Snapshot на `as_of` не может содержать ничего, что стало известно позже (проверяется в `StructureSnapshot.__post_init__`).
- `as_of` приводится к закрытию последнего бара таймфрейма (`floor`). Незакрытые свечи исключаются политикой Phase 2; `as_of` внутри формирующегося бара → STALE.

## Алгоритмы v1

### Swing (fractal-v1)
Бар `i` — swing high, если `high[i]` **строго выше** каждого из `swing_left` предыдущих максимумов и **не ниже** каждого из `swing_right` следующих. Swing low — зеркально.
Равные экстремумы: из равных максимумов в пределах `swing_left` баров swing — **самый ранний** (строгое сравнение слева, нестрогое справа). Равные экстремумы дальше друг от друга — два отдельных swing и кандидаты в EQH/EQL.
Подтверждение: закрытие бара `i + swing_right` (по умолчанию 2/2).

### Структурные уровни и пробой
- Структурный максимум — **последний** подтверждённый swing high; структурный минимум — последний подтверждённый swing low. Каждый уровень может быть пробит **один раз**; повторного события на следующих барах нет.
- Метод пробоя (`break_confirmation`): `close` (по умолчанию) — бар закрывается строго выше/ниже уровня; `wick` — отдельный, явно выбираемый метод: бар торгуется строго за уровнем. Молча один метод другим не подменяется.
- В одном баре сначала проверяется бычий пробой, затем медвежий (детерминированный порядок).

### BOS vs CHoCH
Направление структуры — результат последнего пробоя в окне (UNKNOWN до первого пробоя).
| Предыдущее направление | Бычий пробой | Медвежий пробой |
|---|---|---|
| UNKNOWN | `break_unclassified` (reason `DIRECTION_UNKNOWN_FIRST_BREAK_IN_WINDOW`), направление → bullish | `break_unclassified`, направление → bearish |
| BULLISH | **BOS** (продолжение) | **CHoCH** (смена характера) |
| BEARISH | **CHoCH** | **BOS** |
Это разные правила: один и тот же геометрический пробой получает разную метку в зависимости от предыдущего направления (тест с зеркальным путём). При неизвестном направлении метка BOS/CHoCH не присваивается. «Конфликтующая структура» (оба уровня пробиты в одном баре) даёт два события в документированном порядке; итоговое направление — медвежье.

### Equal highs / equal lows
- Допуск: `eq_tolerance_atr × ATR(atr_feature)`, где ATR вычисляется Feature Engine **на момент подтверждения** второго экстремума (point-in-time, с политикой качества Phase 2). Единицы — цена котировки, масштаб задаёт волатильность самого инструмента и таймфрейма.
- ATR не VALUE → уровень **не формируется**, reason code `EQ_TOLERANCE_UNAVAILABLE`; fallback-допуска нет.
- Формирование: новый подтверждённый экстремум сначала пробует присоединиться к активному EQ-уровню того же типа (разница с **эталонной ценой первого участника** ≤ допуска уровня, расстояние от последнего участника ≤ `eq_max_separation_bars`). Иначе он образует новый уровень с самым поздним подходящим активным swing. Цепного «сползания» нет: сравнение всегда с эталоном, а не с последним участником.
- Цена уровня — внешняя граница (max для EQH, min для EQL), зона `[min, max]` участников. Участники-swing получают статус `merged` и не учитываются повторно.

### Liquidity levels и статусы
- Buy-side: над каждым подтверждённым swing high и над EQH. Sell-side: под swing low и под EQL.
- Статусы:
  - **active** — уровень создан, взаимодействия ещё не было.
  - **swept** — бар торговался **строго** за уровнем и **закрылся на исходной стороне** (или ровно на уровне) в том же баре.
  - **invalidated** — бар **закрылся** за уровнем (цена «принята», это не sweep).
  - **expired** — `level_expiry_bars` баров без взаимодействия.
  - **merged** — swing-уровень поглощён EQ-уровнем.

  Касание (`high == level`) — не sweep и не invalidation. Все статусы, кроме active, терминальные.
- Sweep подтверждается закрытием бара-свипа; многобаровый возврат (проникновение и возврат через несколько баров) в v1 **не** считается sweep — см. ограничения.

## Data quality (политика Phase 2, без альтернатив)
- Структура считается на последнем **непрерывном** отрезке длиной ≥ `min_bars`, максимум `window_bars` последних баров («память» структуры). События не пересекают дыры: при дыре внутри требуемого окна → `data_quality = gap`, структуры нет; если дыра старше окна — структура начинается после неё (`HISTORY_BEFORE_LAST_GAP_IGNORED`).
- INSUFFICIENT_HISTORY / MISSING_HISTORY / STALE / DUPLICATE / INVALID → snapshot без swings, событий и уровней (доменный инвариант), с reason codes.

## Конфигурация (`config/structure_v1.json`) — начальные исследовательские параметры
| Ключ | Значение | Смысл |
|---|---|---|
| swing_left / swing_right | 2 / 2 | строгость фрактала / задержка подтверждения |
| break_confirmation | close | `close` или `wick` |
| min_bars | 100 | минимум непрерывных баров |
| window_bars | 500 | память структуры |
| atr_feature | atr_14 | Phase 2 feature для допуска (только реализованная, не proxy, в ценовых единицах) |
| eq_tolerance_atr | 0.1 | допуск EQH/EQL в долях ATR |
| eq_max_separation_bars | 100 | максимум баров между равными экстремумами |
| level_expiry_bars | 300 | срок жизни неактивированного уровня |
| snapshot_max_swings / recent_events / recent_sweep_bars | 10 / 20 / 96 | объём snapshot |
Все ключи обязательны, значения валидируются; в `engine.py` нет числовых порогов (тест).

## Persistence (`0006_structure.sql`)
- `structure_snapshots` — неизменяемые snapshots; ключ `(symbol, tf, as_of, structure_version, config_hash, input_fingerprint)`; тот же ключ с другим результатом → `StorageError`.
- `structure_events` — append-only журнал BOS/CHoCH/unclassified/sweep. `event_id` = хэш того, **что произошло** (уровень, бар, метод, версия, конфиг), а не метки. Поэтому то же событие записывается один раз.
- `structure_event_conflicts` — если тот же пробой позже получил другую метку (например, окно памяти сдвинулось за событие, задававшее направление), первая запись остаётся авторитетной, а расхождение сохраняется, но не перезаписывает её.
- Monitor-таблицы для структуры не используются: результат шага виден в health (`units.*.structure`, `structure.*`) и в логе.

## Monitor
После сохранения RegimeSnapshot выполняется шаг структуры. Статусы шага: `ok`, `unavailable` (качество данных), `error`, `persistence_error`, `retry_exhausted`. Ошибка структуры не меняет статус и snapshot режима. Повтор на том же баре выполняет **только** структуру, не более `unit_max_attempts` раз. Отключение — `monitor run --no-structure`.

## CLI
```
python -m app structure analyze   --symbol ETHUSDT --tf 15m [--at ISO] [--save] [--json]
python -m app structure last      --symbol ETHUSDT --tf 15m [--json]
python -m app structure events    --symbol ETHUSDT --tf 15m [--kind bos choch sweep] [--limit 30]
python -m app structure liquidity --symbol ETHUSDT --tf 15m
python -m app structure status
```

## Ограничения v1
- Фракталы 2/2 на 15m чувствительны к шуму: на синтетическом случайном блуждании CHoCH почти так же част, как BOS. Сами по себе события — описание структуры, а не сигналы.
- Структурный максимум/минимум — последний swing (без «защищённого минимума», без внутренней/внешней структуры и без фильтра значимости).
- Sweep — только однобаровый (проникновение и возврат в одном баре).
- Память структуры ограничена `window_bars`: пересчёт на другом окне может классифицировать старый пробой иначе. Это фиксируется в `structure_event_conflicts`, а не прячется.
- EQ-кластер использует допуск на момент формирования; изменение волатильности после этого не пересчитывает уровень.
- Order book, стены L2 и реальная ликвидность — вне этой фазы.
- MTF-агрегации структуры нет: snapshots независимы по таймфреймам.
