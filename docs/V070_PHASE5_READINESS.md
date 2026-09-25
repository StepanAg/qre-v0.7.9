# Phase 5 Readiness

## Что готово
- Монитор, который держит в актуальном состоянии свечи, режимы и BTC-контекст для набора символов и таймфреймов, с явными статусами, health и историей событий.

## Что нужно до/в Phase 5
1. **Реальный 2-часовой smoke run** на Bybit public data (инструкция — в отчёте Phase 4). Проверить: отсутствие массового WAITING_FOR_BAR/STALE (достаточен ли `bar_close_grace_s`), число запросов, отсутствие ошибок persistence, распределение режимов.
2. **Анализ распределения режимов** по реальным данным: не слишком ли часто TRANSITION/HIGH_VOLATILITY при порогах v1. Если да — это вход для regime v2 (hysteresis), а не повод менять v1 задним числом.
3. **Формат отчёта для наблюдателя** (что показывать человеку во время запуска): сейчас это `monitor health` и `monitor last`.

## Чек-лист реального smoke run (2 часа)
Перед запуском:
- `python test.py` → PASS; `python test.py --network` → `Bybit Network Smoke` PASS (доступ к api.bybit.com есть).
- В `.env` нет live/demo-флагов (иначе старт откажет — это правильно).

Во время запуска (второй терминал):
- `python -m app monitor health` — `heartbeat_age_s` < 2 × `heartbeat_interval_s`, `api_status = ok`.
- `python -m app monitor last --symbol BTCUSDT` — `as_of` совпадает с последним закрытым баром.

После запуска — что считать успехом:
| Проверка | Ожидание |
|---|---|
| `summary.status` | `completed` (или `stopped`, если остановлен вручную) |
| `cycles_with_errors` | 0 или единичные, с объяснимыми причинами в `monitor_events.detail` |
| `snapshots_created` за 2 часа, 3 символа | 15m: 8 баров × 3 = 24; 1h: 2 × 3 = 6; 4h: 0–3 (зависит от времени старта) + стартовые |
| `snapshots_duplicate` | 0 в пределах одного запуска |
| `waiting_for_bar` / `stale` | единичные `waiting_for_bar` допустимы; `stale` — повод увеличить `bar_close_grace_s` |
| `api_requests` | порядка десятков (прогрев + 1–2 запроса на новый бар ряда), не тысяч |
| `data_quality_failure` | 0 для BTC/ETH/SOL; иначе смотреть gap в `data gaps` |

Что сохранить для анализа: `var/logs/monitor-*.log`, вывод `monitor health`, выборку `monitor_events` и `regime_snapshots` за запуск.

## Вердикт
**READY** для следующей фазы при условии, что пункт 1 будет выполнен и его результаты проанализированы.
