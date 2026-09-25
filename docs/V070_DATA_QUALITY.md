# QRE v0.7.0 — Data Quality

## Принцип
Некорректные данные никогда не попадают в БД молча и никогда не превращаются в пустой датасет. Каждая проблема — это либо исключение, либо `RecordIssue`, сохранённый в `data_quality_issues` и отражённый в статусе прогона.

## Два класса ошибок
| Класс | Пример | Реакция |
|---|---|---|
| Структурная (`MalformedResponse`) | не JSON, HTML вместо JSON, нет `result.list`, строка неверной формы, чужой symbol/category | весь ответ отвергнут, исключение |
| Содержательная (`RecordIssue`) | `"abc"`/`NaN` в цене, high < close, невыровненное время | запись отвергнута, issue, статус `completed_with_issues`, образовавшаяся дыра видна gap detection |
| API (`BybitApiError`) | retCode ≠ 0 | исключение; повтор только для 10006/10016/10429 |
| Хранилище (`StorageError`) | нет таблицы, нарушение триггера | исключение, прогон помечен `failed` (если это возможно записать) |

`except Exception: return []` в проекте нет; единственный широкий `except` в `BackfillService` записывает прогон как `failed` и **пробрасывает** исключение дальше.

## Инварианты свечи (`Candle.__post_init__`)
`high ≥ low`, `high ≥ max(open, close)`, `low ≤ min(open, close)`, цены > 0, `volume ≥ 0`, `turnover ≥ 0`, `open_time` выровнен по таймфрейму, время UTC-aware, значения Decimal и конечны.
Допустимое исключение: бар без сделок (`O=H=L=C`, volume = turnover = 0) — Bybit так отдаёт интервалы без торговли.

## Дубликаты
| Уровень | Механизм |
|---|---|
| Внутри ответа / между страницами | `dedupe()`: идентичные схлопываются (`duplicates_in_response`); разные значения → оба отброшены (`conflicting_duplicate`) |
| Против сохранённых | сравнение перед вставкой: одинаковые → `unchanged`; разные → `stored_conflict`, сохранённое не меняется |
| БД | PRIMARY KEY + триггер неизменяемости; прямой повторный INSERT → `IntegrityError` (тест) |

## Порядок
Bybit отдаёт от новых к старым. Строго монотонный порядок в любую сторону нормален; иной порядок даёт issue `out_of_order`, данные всё равно сортируются по возрастанию.

## Gap detection
`find_gaps(times, timeframe, expected_start, expected_end)` учитывает таймфрейм и находит дыры в начале, в середине и в конце диапазона. Время до листинга (`instruments.launch_time`) дырой не считается. Дыры внутри торговой истории perpetual (обслуживание биржи, отсутствие ответа) **обнаруживаются и сообщаются, но не заполняются** — интерполяция цен запрещена.

## Проверка единиц
VWAP = turnover / volume обязан лежать в [low, high] (допуск 0.5 %). Нарушение → `unit_suspect`. Это прямая защита от прошлой путаницы CVD/volume.

## Покрытие тестами
Fixtures в `tests/fixtures/bybit/` в реальном формате Bybit v5: normal, empty, with_open, api_error 10001/10006, malformed (HTML), truncated JSON, missing list, bad row shape, duplicates (идентичные + конфликтующие), out_of_order, gap, invalid_number, bad_ohlc, misaligned, units_swapped, wrong_symbol, page1/page2, instruments linear (+2 страницы) и spot, tickers, funding, server_time.
Для многостраничных сценариев используется `SyntheticBybit` — эмулятор семантики kline API (newest-first, лимит, inclusive start/end, формирующийся бар, момент листинга, пропуски, битые значения).
