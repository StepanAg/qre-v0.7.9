# QRE v0.7.0 — Feature Engine (Phase 2)

```
MarketDataStore (port) ─► FeatureService ──► FeatureEngine ──► FeatureSnapshot
   (closed candles)          │  loads exactly     │  DataQualityPolicy.prepare (point-in-time)
                             │  required bars     │  per feature: assess → Window → fn → finiteness
                             └─ cache (FeatureSnapshotStore, keyed by inputs)
```

## Компоненты (`app/research/`)
| Модуль | Ответственность |
|---|---|
| `quality.py` | `DataQualityPolicy`, `ValidatedSeries` (float-массивы, разрывы, stale), `Assessment` |
| `features/spec.py` | `FeatureSpec` (метаданные + требования + формула), `FeatureRegistry`, `Window`, formula hash |
| `features/methodology.py` | EMA/Wilder/std/TR и критерий warmup — общий для всех формул |
| `features/catalog.py` | 38 спецификаций v1 (37 реализованы, `cvd` — NOT_IMPLEMENTED) |
| `engine.py` | `FeatureEngine.evaluate / snapshot / snapshot_mtf / coverage`, хэши |
| `alignment.py` | правила MTF: `last_closed_open_time`, `is_known_at` |
| `service.py` | загрузка из `MarketDataStore`, кеш снапшотов; сети не касается |
| `ports.py` | `FeatureCalculator`, `FeatureSnapshotStore`, `RegimeClassifier` (Phase 3) |

Почему одна спецификация на feature, а не класс: формула — чистая функция `f(window, **params)`. Её можно тестировать напрямую, хэшировать и переиспользовать с разными параметрами (`ema_20/50/200` — одна функция). Метаданные и требования — данные, а не код.

## Контракт формулы
Формула получает `Window` ровно из `min_observations` последних **непрерывных закрытых** баров, заканчивающихся в `as_of`. Больше она ничего не видит: ни БД, ни будущих баров, ни соседних таймфреймов. Возвращает `float` или `NotAvailable(reason)`. Всё остальное — ошибка программиста (`TypeError`).
После формулы движок проверяет результат: исключения `ZeroDivision/Value/Overflow` → INVALID с причиной; NaN/±inf → INVALID.

## FeatureSnapshot
`symbol`, `as_of`, `timeframe` (базовый), `features` (ключ `name` для базового TF, `"<tf>:<name>"` для остальных), `feature_set_hash` (имена + версии + formula hash + TF), `input_fingerprint` (точные входные бары: время и Decimal-значения), `engine_version`. Строгий доступ `get(name)` бросает исключение на неизвестном имени или не-VALUE.

## Multi-timeframe
`snapshot_mtf` готовит **каждый** таймфрейм на **одном и том же** `as_of`. Старший бар, не закрывшийся к `as_of`, физически не попадает в его ряд. Для 15m в 15:15 используется 1h-бар 14:00–15:00 и 4h-бар 08:00–12:00; 1h-бар 15:00 появится только в 16:00. Если ожидаемого старшего бара нет — STALE, а не подстановка более старого.

## Versioning
Ключ feature — `name:vN`. `formula_hash` = sha256(исходник формулы + исходники всех вызываемых ею функций проекта + params + min_observations). `VERSIONS.lock` фиксирует хэш каждой версии; тест падает, если формула изменилась без новой версии. Правило: изменил формулу → зарегистрируй `vN+1`, старая версия остаётся доступной (`registry.get("ema_200@1")`).

## Persistence и кеш
Миграция `0003_features.sql`: `feature_snapshots` (заголовок, UNIQUE по symbol/tf/as_of/feature_set_hash/input_fingerprint) и `feature_values` (одна строка на feature).
Почему нормализованные строки, а не JSON: у каждого значения свой статус, качество, версия, единица и причина. Их нужно фильтровать (coverage, выборки для обучения) и ограничивать `CHECK (status='value') = (value IS NOT NULL)`. Внутри JSON эти ограничения были бы невидимы для БД.
Значения — `REAL` (IEEE double, точный round-trip; проверено по битам). Строки неизменяемы (триггеры). Повторное сохранение идентичного снапшота — no-op; тот же ключ с другими значениями → `StorageError` («недетерминизм»).
Кеш: `FeatureService` считает ключ из фактических входных баров. Если данные изменились (например, дыру дозагрузили), ключ другой — устаревший результат не вернётся.

## Incremental / производительность
Сейчас — простой детерминированный калькулятор: каждый снапшот считает каждую формулу на её точном окне (EMA-200: 800 шагов). Полный снапшот из 37 features — около 8 мс; coverage 7 features × 1500 точек — около 0.2 с. Инкрементальный пересчёт (переиспользование EMA между соседними точками) сознательно не сделан: при фиксированном окне он дал бы другой результат, чем расчёт с нуля, и сломал бы детерминизм. Если понадобится для бэктеста, делать через кеш снапшотов или векторизацию, с тестом эквивалентности.

## CLI
```
python -m app features catalog
python -m app features snapshot --symbol BTCUSDT --tf 15m --mtf 1h 4h [--at 2026-03-01T12:15+00:00] [--save]
```

## Безопасность
Research импортирует только `app.domain`, `app.core`, `app.research`, `app.data.ports`, `app.data.quality` (AST-тест `ResearchSafetyTests`). Нет сети, приватного API, ордеров, риск-лимитов, Telegram, AI.
