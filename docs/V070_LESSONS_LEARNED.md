# QRE v0.7.0 — Lessons Learned

Формат: **проблема в старой системе → причина → как предотвращено в v0.7.0 → чем проверено.**

| # | Проблема | Корневая причина | Решение в v0.7.0 | Проверка |
|---|---|---|---|---|
| 1 | Phantom trades | Сделка создавалась из сигнала/ордера, а не из исполнения | Trade существует только как агрегат fills; Signal/Order/Trade — разные типы | `test_signal_order_trade_are_distinct_types`, `test_metrics_exclude_open_trades` |
| 2 | Duplicate closes | Закрытие регистрировалось из разных источников (WS + REST + локальный SL) | Дедуп по `exec_id` (PK в БД, no-op в домене); выход > открытого объёма → ошибка | `test_duplicate_fill_is_idempotent`, `test_phantom_or_double_close_rejected`, `test_fill_dedup_and_append_only` |
| 3 | Потеря partial fills | Сделка моделировалась как пара entry→exit | Order → N fills; Trade → N ног; `filled_qty` выводится из fills | `test_partial_fills_and_partial_closes`, `test_filled_qty_from_fills_dedups` |
| 4 | Maker timeout | Ожидание maker-исполнения без явного дедлайна и сценария «частично исполнен» | `EntryPlan.expires_at` + `fallback_to_market`; статус `PARTIALLY_FILLED → CANCEL_REQUESTED` учтён в автомате | таблица переходов Order |
| 5 | Protective SL | SL ставился на плановый объём, а не на фактически исполненный | SL будет привязан к `Position` (фактический объём из fills), а не к Order; перенос SL — PositionEvent | Phase 5 (архитектурно заложено) |
| 6 | Reconciliation | Одна «истина» в памяти процесса | Local и Exchange state разделены (`source`, `reconciliation_status`, `exchange_snapshots`, `reconciliation_runs`) | схема БД |
| 7 | Restart recovery | Состояние жило в памяти | Всё критичное в SQLite; `client_order_id` сохраняется до отправки; `non_terminal()` для сверки | `test_restart_recovery_rebuilds_state` |
| 8 | PnL decomposition | Одно перезаписываемое число PnL | Колонки PnL нет; `PnLBreakdown` из ног и funding; проскальзывание не считается дважды | `test_pnl_decomposition_with_funding_and_slippage` |
| 9 | Funding | Не учитывался или дублировался | `FundingEvent` со знаком, дедуп по `exchange_ref`, привязка к trade | тот же тест |
| 10 | Fee accounting | Комиссия считалась стратегией/по формуле | Комиссия — поле конкретного fill (из отчёта биржи), ребейт допустим | `test_partial_fills_and_partial_closes` |
| 11 | Некорректный R | R пересчитывался от перенесённого стопа | `initial_stop` заморожен (frozen dataclass + DB-триггер); R от фактических ног | `test_initial_risk_uses_frozen_stop_and_actual_fills`, `test_initial_stop_immutable_in_db` |
| 12 | ATR fallback | Подстановка «запасного» ATR маскировала нехватку данных | `Feature.is_valid` + обязательная причина; `FeatureSnapshot.get` бросает исключение | `test_feature_insufficient_history_cannot_be_valid` |
| 13 | Мало свечей для EMA | EMA на коротком ряду считалась «валидной» | Нельзя создать валидную фичу при `bars_used < lookback`; Phase 1 требует прогрев истории | тот же тест; Phase 1 spec |
| 14 | CVD normalization | Сырой CVD сравнивался между символами/таймфреймами | Фича несёт `timeframe`, `version`; нормализация — ответственность FeatureEngine (Phase 2) с явным методом | Phase 2 |
| 15 | Неправильное использование feature fields | Доступ к полям по опечатке/`dict.get(..., 0)` | Строгий `get()` с `KeyError` на неизвестное имя | тот же тест |
| 16 | Blocking maker wait | `sleep` в основном цикле блокировал обработку остальных событий | Дедлайн как данные (`expires_at`); execution — событийный, без sleep | Phase 5 |
| 17 | Exchange/local mismatch | Локальная позиция расходилась с биржевой, без детектора | Статусы `MISMATCH/LOCAL_ONLY/EXCHANGE_ONLY`; Position с `source`; `Fill.local_order_id=None` для чужих исполнений | модели + схема |
| 18 | Float-ошибки в деньгах | `float` в ценах/PnL | Только `Decimal`, `float` отвергается, в БД TEXT | `test_float_rejected` |
| 19 | Тесты зависели от аккаунта | Тесты ходили на биржу с ключами | Тесты без сети и ключей; запрет сетевых библиотек в Phase 0 | `test_no_network_libs_outside_adapters` |
| 20 | Фиктивный PASS | Наличие файла считалось реализацией | Реестр компонентов + самопроверка раннера (пустой модуль = FAIL) | категория «Test infrastructure» |

## Что намеренно не перенесено
Ни один файл, класс, функция или схема БД старого проекта. `main.py` со всей логикой не воспроизводится — CLI тонкий, логика в слоях.

## Phase 1 (Market Data)
| Проблема старой системы | Решение | Проверка |
|---|---|---|
| CVD/volume confusion | `volume` = base, `turnover` = quote, документировано; VWAP-проверка единиц | `test_swapped_units_detected` |
| EMA на коротком ряду | длина истории = требования feature (`bars_per_value`, EMA-200 = 801), `ensure_history` → `InsufficientHistory` | `WarmupTests` |
| Wrong feature field / строки таймфреймов | единый `Timeframe`, `"15"`/`"900"` отвергаются, коды Bybit только в адаптере | `test_timeframe_single_representation` |
| Silent data loss | структурные ошибки = исключения; битые записи = issue + статус; `failed` прогон записывается и ошибка пробрасывается | `FailureHandlingTests` |
| Duplicate data | PK в БД + dedup в приложении + конфликт не перезаписывает | `DuplicateDetectionTests`, `IdempotencyTests` |
| Restart problems | всё в SQLite, повторный backfill идемпотентен | `test_restart_keeps_data` |
| Concurrent SQLite writes | `SerializedWriter` | `test_concurrent_producers_serialised` |
| Незакрытая свеча в истории | `is_closed` по времени сервера, clamp диапазона, Sink отвергает открытые | `test_open_candle_*` |

## Phase 2 (Features)
| Проблема | Решение | Проверка |
|---|---|---|
| EMA-200 без прогрева | окно = seed + warmup по критерию e⁻⁶ (800 баров); иначе INSUFFICIENT_HISTORY | `test_ema200_warmup`, `test_ema200_not_computed_on_last_200_only` |
| ATR fallback 1.5 % | нет fallback; статус с причиной; тест на отсутствие констант | `ATRTests`, `test_atr_failure_has_status_not_fallback` |
| Coin volume выдавался за CVD | `cvd` = NOT_IMPLEMENTED; proxy явно помечен | `CVDNamingTests` |
| Feature field mismatch | строгий `get()`, единицы в спецификации и в каждом значении, ряд с чужим symbol/TF = INVALID | `test_foreign_symbol_or_timeframe_is_invalid`, `test_feature_values_have_declared_unit_and_version` |
| Склейка разорванного ряда | только последний непрерывный участок; доходность через дыру не считается | `NoSilentFillTests` |
| Look-ahead | фильтр close_time ≤ as_of до построения окна | `LookAheadTests` |
| Незакрытые старшие свечи | все TF готовятся на одном as_of; нет старшего бара → STALE | `MTFAlignmentTests` |
| 698/700 из Phase 1 | старая дыра = MINOR_GAP и не блокирует; свежая блокирует до восстановления | `test_old_gap_is_minor_the_phase1_698_of_700_case`, `test_recovery_after_enough_new_bars` |

## Phase 2 (Features) — найдено при ревизии
| Проблема | Решение | Проверка |
|---|---|---|
| Две методологии прогрева (data: 3N; engine: N + 3N) → backfill «ok», EMA-200 недоступна везде | длина истории выводится только из `FeatureSpec.min_observations` | `test_planned_history_is_enough_for_every_usable_point` |
| Режим допуска дыр склеивал ряд (формула не знает о дыре) | запрещён до появления окна с маркерами дыр | `test_gap_tolerance_disabled_even_with_rationale` |

## Phase 3 (Regime)
| Риск | Решение | Проверка |
|---|---|---|
| Режим «считается» на плохих данных | любой required ≠ VALUE → UNKNOWN с причиной, metrics пустые | `RegimeDataQualityTests` |
| Скрытые магические числа | все пороги в JSON, в `rules.py` нет float-литералов, `config_hash` в snapshot | `RegimeConfigTests` |
| Псевдоточная уверенность | confidence отсутствует, strength категориальный | модель + доки |
| MTF-конфликт замаскирован | `mtf_alignment` + коды `MTF_CONFLICT` | `MultiTimeframeTests` |
| Незакрытый старший бар | общий `as_of` + point-in-time Feature Engine | `RegimeLookAheadTests` |

## Phase 4 (Monitor)
| Риск | Решение | Проверка |
|---|---|---|
| Blocking wait / tight loop | один `poll_interval_s`, прерываемое ожидание, backoff единицы | `test_temporary_api_error_then_recovery` |
| Одна ошибка роняет весь монитор | статус на единицу; старшие TF и BTC не блокируют базовый анализ | `test_one_unavailable_timeframe_does_not_stop_others` |
| Повторный анализ того же бара | `last_processed_as_of` + идемпотентный store | `test_hour_of_polling_creates_one_snapshot_per_bar` |
| Бесконечный backfill | прогрев один раз на ряд, затем догрузка | `test_warmup_insufficient_does_not_block_others` |
| «SUCCESS» при сбое записи | PERSISTENCE_ERROR, persisted=failed | `test_persistence_failure_is_reported…` |
| Потеря состояния при остановке | finish_run в `finally`, heartbeat в БД | `MonitorLifecycleTests` |

## Phase 5 (Structure)
| Риск | Решение | Проверка |
|---|---|---|
| Swing «известен» в момент экстремума | pivot_time ≠ confirmed_at; доменный запрет фактов позже as_of | `NoLookAheadTests`, `test_not_known_before_confirmation` |
| Любой пробой = BOS | метка зависит от предыдущего направления; unknown → unclassified | `test_mirror_same_bars_opposite_labels` |
| Ложный пробой фитилём | close-метод по умолчанию, wick — явная альтернатива | `test_wick_without_close_is_not_a_break` |
| Фиксированный допуск для всех монет | допуск в ATR на момент формирования; нет ATR → нет уровня | `EqualLevelTests` |
| Касание = sweep | строгое проникновение + возврат закрытием | `test_touch_is_not_a_sweep` |
| Переписывание истории новой интерпретацией | event_id по факту, конфликты в отдельной таблице | `test_reclassification_keeps_first_record` |
| «Ликвидность» из свечей выдаётся за стакан | явные формулировки в модели, CLI и доках | `test_no_order_book_claims` |

## Phase 6 (Setups)
| Риск | Решение | Проверка |
|---|---|---|
| Сетап = сигнал | явный дисклеймер, нет полей tradeable/score | `test_not_a_signal_statement`, `test_domain_separates_notions` |
| Ретроспективное подтверждение | replay только до as_of, структура на каждом баре = живой расчёт | `PointInTimeTests`, `test_contexts_link_to_live_snapshots` |
| Сетап на каждом тике из одного события | триггер только при `event_time == close_j`, id по факту, UNIQUE(setup_id, status) | `test_bar_by_bar_replay_records_each_fact_once` |
| «Касание» после BOS как pullback | обязательный уход от уровня до возврата | `test_bar_after_bos_is_not_an_automatic_touch` |
| Режим тихо фильтрует сетапы | режим = отдельное поле, жизненный цикл от него не зависит | `test_regime_does_not_change_lifecycle_only_fit` |

## Phase 7 (Research)
| Риск | Решение | Проверка |
|---|---|---|
| Replay ≠ живой расчёт | replay вызывает те же сервисы на тех же свечах | `test_identical_to_live_point_in_time_evaluation` |
| Исполнение по цене закрытия бара решения | вход только на открытии следующего бара | `test_entry_at_next_open_not_decision_close` |
| «Лучший» сценарий при стопе и цели в одном баре | берётся стоп, помечено | `test_stop_and_target_in_one_bar_takes_the_stop` |
| Двойной учёт издержек | учёт Phase 0 + инвариант SimulatedTrade | `test_slippage_is_attributed_not_double_counted` |
| Цензурированный горизонт как ноль | статусы censored/incomplete без значений | `test_censored_is_not_zero` |
| Дыра + конец данных = «просто конец истории» | дыра в доступной части приоритетнее | `test_gap_inside_horizon_is_incomplete` |
| Молча изменившиеся данные | fingerprint + verify → новый датасет | `test_changed_source_data_is_detected` |
| Утечка между выборками | хронология, purge, embargo | `SplitTests` |

## Phase 8 (Analytics)
| Риск | Решение | Проверка |
| --- | --- | --- |
| Аналитика пишет в БД | `mode=ro` + SELECT-only + снимки таблиц | `test_connection_cannot_write`, `test_analytics_reader_is_read_only`, `test_every_command_runs_and_is_read_only` |
| Breakeven как убыток | отдельная категория; Phase 7 отчёты не трогаются | `test_hand_computed_performance` |
| Невозможные доли в воронке | `count_exceeds_previous_step` | `test_research_funnel_by_hand` |
| Equity-метрики на отфильтрованных сделках | `equity_not_filterable` | `test_values_through_the_cli` |
| Wall clock в отчётах | as_of = максимум данных; stale-монитор по данным | `test_json_is_deterministic_and_as_of_comes_from_data` |
| Лишние поля конверта | фиксированный контракт | `test_every_command_runs_and_is_read_only` |

## Phase 9 (Strategy + Risk)
| Риск | Решение | Проверка |
| --- | --- | --- |
| Look-ahead через поля наблюдения, вычисленные на last_seen | `SetupView`: только переходы ≤ момента решения, без contradictions | `test_view_is_point_in_time` |
| Незаметное изменение Phase 7 | подкласс вместо правки v1; эталонные хэши до изменений | `Phase7CompatibilityTests` |
| «Почти эквивалентно» (Decimal против float) | арифметика опорных уровней как в Phase 7 | `EquivalenceTests` (нашёл расхождение в pullback) |
| Проверка минимумов до уменьшения | порядок: уменьшение → минимумы | `test_leverage_reduces_then_minimums_rechecked` |
| Пустой тест «зелёный» | тест открытой позиции переписан на два символа | `test_open_position_is_visible_and_not_closed_by_risk` |
