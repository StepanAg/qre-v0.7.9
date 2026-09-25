# Phase 7 Readiness

## Что доступно
На `(symbol, timeframe, as_of)` — `SetupSnapshot`: сетапы (тип, направление, статус и полная история переходов, ключевой уровень, условие невалидности, доказательства, противоречия, соответствие режиму), ссылки на Regime/Structure snapshots, версии и fingerprint. Журнал `setups` / `setup_events` для исследований.

## Что нужно решить до следующей фазы
1. **Реальный прогон** монитора со всеми шагами (`run_monitor.bat --hours 24`) и выгрузка `setup list/events`: частота сетапов по типам и таймфреймам, доля confirmed/invalidated/expired. Это первая реальная статистика движка (не торговая).
2. **P1 из аудита — фиксация версий features** (`atr_14@v1` в structure/setup, required features режима). До любого расширения Feature Engine.
3. **Инфраструктура исследования** (replay/backtest с комиссиями, slippage, funding, MAE/MFE, expectancy, размер выборки) должна появиться раньше любых торговых решений: сетапы без неё оценивать нечем.
4. Если следующим шагом будет Entry/EV: вход строится только на `confirmed` сетапах с `data_quality = valid`; `regime_fit` и `contradictions` — явные входы, а не скрытые фильтры.

## Вердикт
**READY** для исследовательского этапа (пункты 1–3). Для торговых фаз — **NOT READY** до появления research-инфраструктуры и Risk Engine.
