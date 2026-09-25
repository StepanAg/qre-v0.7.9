# Phase 1 — Market Data Layer (спецификация, не реализовано)

## Цель
Надёжные, нормализованные, сохраняемые рыночные данные Bybit (linear USDT perpetual; spot — только на уровне модели) без какой-либо торговой функциональности.

## Объём
1. `app/data/bybit_rest.py` — реализация `MarketDataProvider` через публичные REST v5 эндпоинты: instruments-info, kline, tickers, orderbook, funding history. Без ключей.
2. HTTP-клиент: таймауты, retry с экспоненциальной паузой, уважение rate-limit заголовков, разбор `retCode`. Выбор библиотеки (stdlib `urllib` vs `httpx`) — зафиксировать в ADR; сетевой код только в `app/data/`, правило архитектурного теста обновить точечно.
3. Нормализация: ответ Bybit → `Candle`/`Ticker`/… (Decimal, UTC). Bybit отдаёт свечи в обратном порядке и включает текущую незакрытую — она помечается `is_closed=False` и не сохраняется как закрытая.
4. Хранение: миграция `0002_market_data.sql` — `candles(symbol, category, timeframe, open_time)` PK, `instruments`, `funding_rates`. Upsert идемпотентен.
5. Backfill и прогрев: загрузка истории с пагинацией; минимальная глубина = max(lookback всех фич) × запас (для EMA-200 на 15m ≥ 600 баров). Проверка пропусков (gap detection) и дубликатов.
6. Инкрементальное обновление по закрытию бара; WebSocket — опционально, в конце фазы.
7. CLI: `python -m app data backfill --symbol BTCUSDT --tf 15 --days 30`, `data status`, `data gaps`.
8. Реестр: зарегистрировать `Market Data` → раннер переведёт категорию в PASS/FAIL.

## Тесты
- Юнит: парсинг записанных JSON-фикстур Bybit (без сети), обратный порядок, незакрытая свеча, пустые ответы, ошибки `retCode`, пагинация, gap detection, идемпотентный upsert.
- Онлайн (опционально, `QRE_ONLINE_TESTS=1`): одна реальная загрузка публичных свечей testnet/mainnet. В обычном прогоне — SKIPPED.

## Acceptance
- `python test.py` — Market Data: PASS; остальное не деградировало.
- Backfill 30 дней BTCUSDT 15m без пропусков, повторный запуск не создаёт дублей.
- Ни одного вызова приватных эндпоинтов, ключи не требуются.

## Вне объёма
Фичи, стратегии, ордера, AI, Telegram.
