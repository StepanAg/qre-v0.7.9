# QRE v0.7.0 — Regime Engine (Phase 3)

Regime описывает **наблюдаемое** состояние рынка на момент закрытия последнего бара. Это не прогноз направления цены и не торговый сигнал. LLM не используется.

## Архитектура
```
SQLite (candles) ─► FeatureService (Phase 2, point-in-time, DataQualityPolicy)
                 ─► FeatureSnapshot (base tf + "1h:…", "4h:…" на ОДНОМ as_of)
                 ─► rules.classify_timeframe()  ×  каждый таймфрейм   (чистая функция)
                 ─► RegimeEngine: MTF alignment + BTC context          (чистая функция)
                 ─► RegimeSnapshot ─► SQLiteRegimeSnapshotStore (0004, append-only)
```
| Модуль | Роль |
|---|---|
| `app/domain/regime.py` | `TimeframeRegime`, `BtcContext`, `RegimeSnapshot` + канонический JSON |
| `app/research/regime/config.py` | загрузка/валидация порогов из JSON, `config_hash`, списки required/optional features |
| `app/research/regime/rules.py` | правила v1 для одного таймфрейма |
| `app/research/regime/engine.py` | MTF-агрегация, BTC context, сборка snapshot |
| `app/research/regime/service.py` | загрузка данных через `FeatureService`, сохранение |
| `app/research/regime/version.py` + `VERSION.lock` | контроль версии правил |
| `app/storage/regime.py`, `migrations/0004_regime.sql` | персистентность |
| `app/cli/regime_cmds.py` | `python -m app regime snapshot | history` |
| `config/regime_v1.json` | **единственный** источник порогов |

Research-слой не импортирует config/storage/network (AST-тест), поэтому JSON с порогами загружает CLI, а путь к нему задаёт `REGIME_CONFIG_FILE`.

## Входные features (только Phase 2, без proxy)
| Роль | Feature | Используется как |
|---|---|---|
| required | `ema_50_200_spread` | структура тренда |
| required | `ema_50_slope_10` | импульс |
| required | `dist_ema_50_pct` | смещение цены |
| required | `atr_14_pct` | нормировка всех расстояний |
| required | `vol_ratio_20_100` | состояние волатильности |
| optional | `ret_20` | горизонт (4-й голос) и условие STRONG |
| optional | `vol_20_annualized`, `range_position_20` | только в `metrics`, в правилах не участвуют |

Если любой required ≠ VALUE → `UNKNOWN`, `data_quality` = самый тяжёлый статус среди отказавших (DUPLICATE > INVALID > STALE > GAP > MISSING_HISTORY > INSUFFICIENT_HISTORY), reason code `REQ_FEATURE_UNAVAILABLE:<tf>:<feature>:<status>:<quality>`. Нули и подстановки запрещены; `metrics` при этом пустой.
Отсутствующий optional фиксируется кодом `OPT_FEATURE_UNAVAILABLE…`; правило, которое его использует, просто не применяется (например, тренд не может быть STRONG).

## Правила v1 (для одного таймфрейма)
1. **Нормировка в ATR.** `spread_atr = ema_50_200_spread / atr_14_pct`, `slope_atr = ema_50_slope_10 / atr_14_pct`, `displacement_atr = dist_ema_50_pct / atr_14_pct`, `return_atr = ret_20 / atr_14_pct`. Все величины — «сколько ATR», поэтому один набор порогов применим к разным символам и таймфреймам. `atr_14_pct = 0` → UNKNOWN (`ATR_ZERO`).
2. **Волатильность.** `vol_ratio ≥ vol_ratio_high` → HIGH; `≤ vol_ratio_low` → LOW; иначе NORMAL. Отношение std(20)/std(100) самонормировано: сравнивается с собственной недавней историей символа.
3. **Голоса** ∈ {−1, 0, +1} с нейтральной зоной `[−порог, +порог]` (границы включительно): STRUCTURE (`structure_min_atr`), MOMENTUM (`slope_min_atr`), DISPLACEMENT (`displacement_min_atr`), HORIZON (`return_min_atr`, если есть `ret_20`).
4. **Режим** (порядок приоритета):
   - `HIGH_VOLATILITY` — если волатильность HIGH; направление тренда всё равно вычисляется и сообщается (`HIGH_VOL_OVERRIDES_TREND`);
   - `TRENDING_UP` / `TRENDING_DOWN` — три основных голоса одного знака, и HORIZON не против;
   - `RANGING` — STRUCTURE = 0 и MOMENTUM = 0 (EMA50 ≈ EMA200 и EMA50 плоская); смещение внутри диапазона не мешает;
   - `TRANSITION` — всё остальное: `COMPONENTS_CONFLICT` (есть голоса разных знаков) или `COMPONENTS_INCOMPLETE` (тренд подтверждён не всеми компонентами).
5. **trend_direction**: CONFLICT, если есть оба знака; UP/DOWN при ≥ 2 голосах одного знака без противоположных; иначе NEUTRAL.
6. **trend_strength** (категориальный, без вероятностей): для трендов STRONG, если `|slope_atr| ≥ strong_slope_atr` и HORIZON есть и ≠ 0; иначе MODERATE. Для прочих режимов — NONE.

**Trend persistence** в v1 реализована как согласие разных горизонтов: наклон за 10 баров, EMA50/200 (фактически ~100 баров), 20-барная доходность. Временная устойчивость («сколько баров режим держится») не реализована — для неё нужна либо новая feature, либо история режимов (см. ограничения).

## Пороги (`config/regime_v1.json`) — начальные исследовательские параметры
Не оптимизированы и не заявляются как прибыльные.
| Ключ | Значение | Смысл выбора |
|---|---|---|
| `structure_min_atr` | 1.0 | EMA50 и EMA200 разошлись минимум на один «типичный бар» |
| `slope_min_atr` | 0.5 | EMA50 сдвинулась за 10 баров на ≥ 0.5 ATR |
| `displacement_min_atr` | 0.5 | цена заметно, а не на шум, отошла от EMA50 |
| `return_min_atr` | 1.0 | 20-барная доходность больше одного ATR |
| `strong_slope_atr` | 1.5 | втрое больше минимального наклона |
| `vol_ratio_high` | 1.5 | краткосрочная волатильность на 50 % выше своей базы |
| `vol_ratio_low` | 0.67 | симметрично (≈ 1/1.5) |
| `timeframes` | 15m, 1h, 4h | анализируемые таймфреймы |
| `btc_symbol`, `btc_timeframe` | BTCUSDT, 1h | фон рынка |

Все ключи обязательны; лишние или отсутствующие ключи — ошибка. В коде правил нет ни одного float-литерала (тест). `config_hash` (только значимые ключи) сохраняется с каждым snapshot; новые пороги создают новые snapshots, не трогая старые.

## Multi-timeframe
- Все таймфреймы подготавливаются Feature Engine на **одном** `as_of`. Бар старшего таймфрейма, не закрытый к `as_of`, не входит в его ряд (Phase 2). В 13:30: 15m — данные по 13:30, 1h — по 13:00, 4h — по 12:00. Это отражено в `feature_timestamps` / `data_through`.
- Итоговый `regime` = режим базового таймфрейма. Конфликты не усредняются и не прячутся: `mtf_alignment` = ALIGNED / CONFLICT / MIXED / PARTIAL / SINGLE, reason codes `MTF_CONFLICT:15m=trending_down,1h=trending_up,…`, `MTF_UNAVAILABLE:4h:…`.

## BTC context
Классифицируется теми же правилами на `btc_timeframe` в тот же `as_of`. Статусы: AVAILABLE, SELF (анализируется сам BTC — используется его собственный результат на `btc_timeframe`), STALE, UNAVAILABLE (при этом regime = UNKNOWN, `data_through = null`). Корреляция BTC с анализируемым символом **не** рассчитывается и не предполагается.

## RegimeSnapshot
`symbol, as_of, timeframe, regime, trend_direction, trend_strength, volatility_state, data_quality, reason_codes, timeframes[] (per-tf результат + metrics), mtf_alignment, btc_context, feature_timestamps, code_version, feature_version (engine + feature_set_hash), regime_version, config_hash, input_fingerprint`.
Поля confidence нет: калиброванной методологии вероятности не существует.

## Persistence
Таблица `regime_snapshots` (миграция 0004). Ключ идемпотентности — `(symbol, timeframe, as_of, regime_version, config_hash, input_fingerprint)`. Повторный анализ тех же данных → no-op. Тот же ключ с другим результатом → `StorageError` (нарушение детерминизма). UPDATE/DELETE запрещены триггерами. Полный `payload_json` позволяет восстановить, почему присвоен режим.

## Версии
`REGIME_VERSION = "1"`. Хэш исходников правил хранится в `app/research/regime/VERSION.lock`. Изменение правил без обновления lock-файла валит тест `test_versions_recorded`; после осознанного изменения нужно поднять версию и выполнить `python scripts/lock_regime_version.py`.

## CLI
```
python -m app regime snapshot --symbol ETHUSDT --tf 15m [--mtf 15m 1h 4h] [--at ISO] [--save] [--refresh]
python -m app regime history  --symbol ETHUSDT --tf 15m
```
`as_of` = начало текущего бара базового таймфрейма (последний закрытый бар). Без `--refresh` команда работает только с локальной БД. `--refresh` загружает ровно нужную историю (символ, все таймфреймы, BTC 1h) с публичных эндпоинтов.

## Ограничения v1
- Нет временной устойчивости режима (hysteresis): на границах порогов режим может переключаться от бара к бару.
- Для классификации нужно 800 непрерывных баров на каждом таймфрейме (EMA-200 через spread). Для 4h это около 134 дней; одна дыра выключает таймфрейм на 800 баров.
- Пороги едины для всех символов (нормировка в ATR это смягчает, но не устраняет).
- HIGH_VOLATILITY по отношению std20/std100 ловит всплеск относительно недавнего прошлого. Длительно высокая волатильность со временем станет «NORMAL».
