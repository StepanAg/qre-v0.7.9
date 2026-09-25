# QRE v0.7.0 — Market Monitor Runtime (Phase 4)

Непрерывный монитор: закрытые свечи Bybit (public REST) → Feature Engine → Regime Engine → SQLite. Ордеров, приватных ключей и LLM нет. Импорт модулей ничего не запускает.

## Runtime loop
```
run():  start_run ─► tick ─► [stop? / max runtime?] ─► wait(poll_interval_s) ─► tick ─► … ─► finish_run
tick(): (instruments не синхронизированы → попытка) → для каждой единицы (symbol × base timeframe):
   expected = floor(now − bar_close_grace_s, tf)          # последний ЗАКРЫТЫЙ бар
   expected уже обработан?          → NO_NEW_BAR (без запросов, без анализа, без записи в лог)
   единица в backoff?               → пропуск до next_attempt_at
   sync базового ряда               → ошибка = статус единицы (блокирует только её)
   sync старших TF и BTC 1h         → ошибка НЕ блокирует; видна в snapshot (mtf PARTIAL / btc STALE)
   бар expected есть в БД?  нет     → WAITING_FOR_BAR; спустя stale_after_s → STALE
   Feature Engine (compute_features)→ исключение = FEATURE_ERROR
   Regime Engine  (classify)        → исключение = REGIME_ERROR
   исход по data_quality            → OK / WARMUP_INSUFFICIENT / STALE / DATA_QUALITY_FAILURE
   сохранение (идемпотентно)        → saved / duplicate / failed(PERSISTENCE_ERROR)
   событие в monitor_events + строка лога
heartbeat каждые heartbeat_interval_s → monitor_runs.health_json + строка лога
```

## Синхронизация данных (без бесконечного backfill)
- Единица данных — ряд (symbol, tf), общий для всех единиц анализа; синхронизируется максимум один раз на бар.
- Требование к истории — `RegimeService.bars_per_value()` (из `FeatureSpec.min_observations`, сейчас 801). Число в runtime не зашито (тест).
- Первое обращение к ряду: если окно полностью лежит в БД — сеть не нужна (рестарт); иначе **один** прогревочный backfill окна. Дальше только догрузка от последнего сохранённого бара.
- Мало истории (свежий листинг) → WARMUP_INSUFFICIENT с `bars_required` / `bars_available`; прогрев не повторяется, история растёт сама с каждым новым баром.
- Формирующаяся свеча не сохраняется (Phase 1) и не анализируется: целевой бар всегда `floor(now − grace)`.

## Статусы единицы
| Статус | Смысл | Повтор |
|---|---|---|
| ok | режим посчитан и сохранён (или уже был сохранён) | — |
| no_new_bar | нового закрытого бара нет | считается в `outcome_counts`, не логируется |
| waiting_for_bar | бар закрылся, но REST ещё не отдал | каждый poll, без штрафа |
| stale | бар так и не пришёл за `stale_after_s` | нет (для этого бара) |
| warmup_insufficient | мало непрерывной истории | на следующем баре |
| data_quality_failure | gap / duplicate / invalid в окне | нет (данные те же) |
| api_temporary_error | сеть, 5xx, rate limit — после HTTP-ретраев | да, с backoff |
| api_error | 4xx / retCode 10001 (некорректный запрос) | на следующем баре, с backoff |
| invalid_data | malformed response / нарушение схемы | да, с backoff, ограниченно |
| feature_error / regime_error | исключение движка | да, с backoff, ограниченно |
| persistence_error | запись не удалась; «сохранено» не сообщается | да, с backoff |
| retry_exhausted | `unit_max_attempts` попыток на бар исчерпаны | со следующего бара |

Несуществующий символ (instruments-info не знает) = ошибка конфигурации: символ отключается на весь запуск, запросы к нему не повторяются.

## Retry / recovery — два уровня
1. **HTTP** (Phase 1, `RequestExecutor`): `HTTP_MAX_ATTEMPTS`, экспоненциальный backoff ≤ 8 с, pacing `HTTP_MIN_INTERVAL_MS`, 429 с учётом `X-Bapi-Limit-Reset-Timestamp`. Не ретраятся 4xx, retCode 10001, malformed JSON.
2. **Единица** (Phase 4): после неудачи `next_attempt_at = now + min(unit_backoff_max_s, unit_backoff_base_s·2^(n−1))`. Не больше `unit_max_attempts` попыток на один бар, после чего RETRY_EXHAUSTED и переход к следующему бару. Остальные единицы работают независимо. Tight loop невозможен: минимальный интервал = `poll_interval_s`.

Повтор после сбоя безопасен: свечи и snapshots идемпотентны (UNIQUE-ключи + проверка содержимого). Недетерминизм даёт `StorageError`, а не перезапись.
`api_status`: ok → degraded (последняя операция неудачна) → down (`api_down_after_failures` подряд). Первый успешный запрос возвращает ok.
Если сам цикл (а не единица) падает `unit_max_attempts` раз подряд, запуск завершается со статусом failed.

## Конфигурация (`config/monitor_v1.json`, путь `MONITOR_CONFIG_FILE`)
| Ключ | По умолчанию | Назначение |
|---|---|---|
| symbols | BTCUSDT, ETHUSDT, SOLUSDT | universe |
| timeframes | 15m, 1h, 4h | базовые TF единиц; они же — MTF-контекст режима |
| poll_interval_s | 30 | частота пробуждения (не анализа); не может превышать наименьший TF |
| bar_close_grace_s | 10 | задержка после закрытия до ожидания бара в REST |
| stale_after_s | 300 | сколько ждать недоставленный бар до STALE |
| heartbeat_interval_s | 60 | heartbeat в лог и в `monitor_runs` |
| unit_max_attempts | 5 | попыток на бар |
| unit_backoff_base_s / max_s | 5 / 120 | backoff единицы |
| api_down_after_failures | 3 | порог api_status = down |
| max_runtime_minutes | null | лимит запуска (можно задать в CLI) |
Все ключи обязательны, скрытых дефолтов нет; в `runtime.py` нет числовых литералов, кроме именованных констант единиц измерения (тест).

## Persistence (миграция `0005_monitor.sql`)
- `monitor_runs` — запуск: конфиг, мета (версии, хэши конфигов, требуемая история), heartbeat, итоговые health и summary, флаг `stop_requested`.
- `monitor_events` — append-only: одна строка на попытку обработки (cycle_id, symbol, tf, as_of, status, regime, data_quality, persisted, duration_ms, detail). Опросы без нового бара не пишутся.
- Регимы — в существующей `regime_snapshots` (Phase 3). UNKNOWN тоже сохраняются: они фиксируют причину.

## CLI
```
python -m app monitor run --hours 2                       # ограниченный запуск
python -m app monitor run --minutes 30 --symbols BTCUSDT ETHUSDT --tf 15m 1h
python -m app monitor run                                 # до Ctrl+C
python -m app monitor health                              # health последнего запуска (+ возраст heartbeat)
python -m app monitor last --symbol ETHUSDT [--tf 15m] [--json]
python -m app monitor stop                                # штатная остановка из другого терминала
```
Ctrl+C: первое нажатие — штатная остановка после текущей операции; второе — немедленно (итоговое состояние всё равно записывается). Лог дублируется в `var/logs/monitor-<время>.log`.

## Ограничения
- Ingestion и анализ последовательные, в одном потоке. 3 символа × 3 TF укладываются в секунды; для десятков символов нужна параллелизация чтения.
- Прогрев 4h требует 801 бар ≈ 134 дня истории — у свежих листингов 4h будет warmup_insufficient.
- Задержка REST после закрытия бара моделируется только grace + stale-окном; WebSocket не используется.
- Реальный длительный запуск на Bybit **не проводился** (в среде разработки нет доступа к api.bybit.com).

## Долгие локальные запуски: сторож (supervisor)
```
run_monitor.bat --hours 24          # Windows, двойной клик = до Ctrl+C
./run_monitor.sh --hours 24         # Linux/macOS
python -m app monitor supervise --hours 24 [--symbols ...] [--tf ...] [--hang-after-min 10] [--allow-sleep]
```
Сторож — отдельный процесс, запускающий `monitor run` как дочерний и следящий за ним снаружи:
| Ситуация | Кто решает | Реакция |
|---|---|---|
| Нет интернета / 5xx / rate limit | монитор | retry с backoff единицы (≤ `unit_backoff_max_s`), `api_status` degraded → down; процесс не перезапускается; после восстановления пропущенные свечи догружаются без дыр (тест: 2 ч обрыва) |
| Компьютер спал | монитор | после пробуждения анализируется последний закрытый бар, пропущенные свечи догружаются (тест: 3 ч) |
| Падение процесса (код ≠ 0, 2) | сторож | перезапуск через 5 с, 10 с … максимум 5 мин; пауза сбрасывается после 10 мин стабильной работы |
| Зависание (нет heartbeat > 10 мин) | сторож | принудительная остановка и перезапуск |
| Ошибка конфигурации/безопасности (код 2) | сторож | **не** перезапускается |
| > 20 перезапусков за час | сторож | останавливается с `gave_up` (нет бесконечного crash-loop) |
| Ctrl+C / `monitor stop` / лимит времени | оба | штатное завершение, без перезапуска |
Коды выхода CLI: 0 — штатно; 2 — конфигурация/безопасность/неверный ввод (не перезапускать); 3 — монитор завершился со статусом failed; 5 — временная ошибка (хранилище, данные).
Сон компьютера на время работы запрещается (Windows — `SetThreadExecutionState`, macOS — `caffeinate`; Linux — не управляется). Экран может гаснуть. Отключить: `--allow-sleep`.
Логи ротируются (10 МБ × 5 файлов): `var/logs/monitor-*.log`, `var/logs/supervisor.log`. Незавершённые запуски (убитый процесс, отключение питания) при следующем старте помечаются `abandoned`, чтобы `monitor health` не показывал мёртвый запуск как работающий.
