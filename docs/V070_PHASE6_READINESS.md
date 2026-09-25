# Phase 6 Readiness (Setup Detection Engine)

## Какие структурные данные теперь доступны
На `(symbol, timeframe, as_of)` — `StructureSnapshot`:
| Данные | Где | Гарантия |
|---|---|---|
| Направление структуры и время его установки | `direction`, `direction_since` | только из подтверждённых пробоев; UNKNOWN не подменяется |
| Последние подтверждённые swings | `swing_highs`, `swing_lows` | `confirmed_at ≤ as_of`, pivot и подтверждение различаются |
| Последние BOS / CHoCH / unclassified | `events`, `last_bos`, `last_choch` | уровень известен до бара пробоя; метод пробоя записан |
| Активные EQH / EQL | `equal_highs`, `equal_lows` | ATR-допуск на момент формирования, исходные экстремумы |
| Активные потенциальные уровни ликвидности | `liquidity` | только ACTIVE; зона, источник, время создания |
| Недавние sweeps | `sweeps` | подтверждены закрытием бара |
| Качество и версии | `data_quality`, `reason_codes`, `structure_version`, `config_hash`, `input_fingerprint` | воспроизводимость |
Плюс журнал событий (`structure_events`) и Regime Snapshot того же бара (Phase 3) для контекста.

## Чего ещё нет (зависимости Setup Detection)
1. **Реальная проверка на Bybit.** Нужен прогон монитора с включённой структурой (`monitor run --hours 2`) и просмотр событий (`structure events`), чтобы оценить частоту событий при фракталах 2/2 на реальных 15m/1h/4h.
2. **Фильтр значимости swings** (например, в единицах ATR) — сейчас любой фрактал является уровнем. Setup Detection, скорее всего, потребует значимых уровней; это будет structure v2 с новой версией.
3. **MTF-комбинирование** структуры (например, 4h-направление + 15m-событие) — решать в Phase 6 явно, на независимых snapshots с общим `as_of`.
4. **Order book / реальная ликвидность** — отдельная будущая фаза; из свечей не выводится.

## Правило для Phase 6
Setup Detection должен читать `StructureSnapshot` и `RegimeSnapshot` только через их модели и на одном `as_of`; при `data_quality ≠ valid` у любого обязательного входа сетап не формируется (с reason code).

## Вердикт
**READY** при условии выполнения пункта 1 до начала Phase 6.
