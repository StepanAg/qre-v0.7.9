# QRE v0.7.0 — Research & Backtest Infrastructure (Phase 7)

> Инфраструктура исследования, а не доказательство прибыльности. Никакая стратегия здесь не проверена out-of-sample, никакие параметры не оптимизировались, никакие результаты не являются торговой рекомендацией. Реальные ордера из этого слоя разместить невозможно.

## Четыре разных режима — разные доказательные требования
| Режим | Что это | Что доказывает | Где в QRE |
|---|---|---|---|
| **Setup Outcome Analysis** | что делала цена после сетапа (MAE/MFE, forward return) | свойства *наблюдений*, не сделок | `research outcomes` (Phase 7) |
| **Simulated Backtest** | гипотетическое тест-правило на OHLCV с моделью издержек | поведение правила в симуляции, с известными упрощениями | `research backtest` (Phase 7) |
| **Paper Trading** | правило в реальном времени без денег, исполнение моделируется на живых данных | устойчивость вне выборки во времени | не реализовано |
| **Live Trading** | реальные ордера | реальные результаты | запрещено (`BUILD_EXECUTION_CEILING = PAPER`) |

## Архитектура
```
SQLite (закрытые свечи, funding) ─► DatasetBuilder ─► ResearchDataset (манифест + fingerprints, 0008)
                                ─► ReplayEngine: для каждого бара t — SetupService.compute(ровно те свечи,
                                   что загрузил бы живой расчёт в t) [+ RegimeService.analyze(t)]
                                ─► ReplayObservations (сетапы, как они были известны)
                                ─► Outcome Analyzer (единственный слой, смотрящий «вперёд»)
                                ─► BacktestEngine (гипотетическое правило; PnL через учёт Phase 0)
                                ─► metrics + chronological splits (purge/embargo)
                                ─► SQLiteResearchStore (immutable runs)
```
| Модуль | Роль |
|---|---|
| `app/domain/research_lab.py` | ResearchDataset, Segment, SeriesCoverage, OutcomeObservation, SimulatedTrade |
| `app/research/lab/config.py` | ResearchConfig, BacktestConfig, CostModel |
| `app/research/lab/versions.py` | версии компонентов (+ хэш каталога features) |
| `app/research/lab/dataset.py` | DatasetBuilder, fingerprint, сегменты, проверка целостности |
| `app/research/lab/replay.py` | ReplayEngine |
| `app/research/lab/outcomes.py` | outcome analysis |
| `app/research/lab/backtest.py` | BacktestEngine |
| `app/research/lab/metrics.py`, `stats.py` | метрики с явным `not_available` |
| `app/research/lab/splits.py` | train/validation/test, purge, embargo |
| `app/research/lab/pipeline.py` | оркестрация |
| `app/storage/research.py`, `migrations/0008_research.sql` | persistence |
| `app/cli/research_cmds.py` | CLI |
Сущности не смешиваются: рыночные данные (`candles`), наблюдения (`replay_observations`), outcomes (`outcome_observations`), симулированные сделки (`sim_trades` — **никогда** не в `trades`), метрики (`metric_reports`). Monitor не импортирует research (тест); длительные задачи запускаются только из CLI.

## Dataset Builder
- Спецификация: символы, таймфрейм, `[start, end)`, опционально контекстные ряды (таймфреймы режима + BTC) для replay с режимом.
- Свечи **не копируются**: таблица `candles` неизменяема, датасет фиксирует их `fingerprint` (хэш всех OHLCV-значений) по каждому ряду и fingerprint funding-ставок.
- Сегменты: непрерывные участки; участок короче требования Setup Engine помечается `usable=false` с причиной `shorter_than_warmup:N<M`. Дыры не заполняются; число пропущенных баров в `quality_summary`.
- `dataset_id` = хэш(спецификация + версии компонентов + конфиги + fingerprints) → детерминирован. Повторная сборка тех же данных — no-op.
- Версии: код, **хэш каталога features** (`features/VERSIONS.lock` — закрывает для исследований P1 аудита о ссылках на features без версии), правила regime/structure/setup с хэшами, config hashes.
- Перед каждым использованием `verify()`: если свечи изменились (например, дыру заполнили позже) или изменилась версия компонента → `DatasetIntegrityError`. Старый датасет и его результаты остаются; нужен новый датасет (новый id).

## Point-in-time Replay
- На каждом баре t `SetupService.compute` получает **ровно** последние `bars_to_load` закрытых свечей ≤ t — то же, что загружает живой расчёт. Тест: replay-snapshot в t побайтово равен `SetupService.analyze` из хранилища в t.
- Режим (опционально, только на датасете с контекстом) — `RegimeService.analyze(t)` по данным ≤ t.
- Кэш структуры — по fingerprint входных баров, поэтому информация из будущего через него пройти не может; добавление «отравленных» будущих свечей не меняет ни одного прошлого snapshot (тест).
- Результат: наблюдения (сетап в последнем известном состоянии + когда впервые увидели + `regime_fit` на тот момент), статистика оценок (сколько баров ушло на прогрев), `replay_hash` — цепочка хэшей всех snapshot: одинаковый replay ⇔ одинаковый хэш.

## Setup Outcome Analysis
- Якорь: закрытие бара `confirmed` (по умолчанию) или `candidate`; P0 = это закрытие. Неподтверждённые сетапы при якоре `confirmed` не анализируются (счётчик `not_confirmed`).
- Горизонты (5/10/20/40 баров) — конфиг. Со знаком направления:
  - long: favorable = high − P0, adverse = P0 − low, return = +(C_h/P0 − 1);
  - short: favorable = P0 − low, adverse = high − P0, return = −(C_h/P0 − 1).
- Значения: forward_return, MFE, MAE (доли P0), бары до MFE/MAE, в ATR (из evidence сетапа): MFE/MAE, закрытие относительно ключевого уровня, достижение порогов k·ATR и что достигнуто первым (`favorable` / `adverse` / `ambiguous` — оба в одном баре / `none`).
- Статусы: `valid`; `incomplete` — дыра внутри доступной части горизонта (приоритетнее конца данных); `censored` — горизонт уходит за конец датасета (с числом доступных баров); `no_anchor`. У невалидных наблюдений **нет** значений (доменный запрет) — не нули.

## Backtest Engine (изолированная OHLCV-симуляция)
Гипотетическое тест-правило (`config/backtest_v1.json`): вход по сетапу при наступлении `entry_on` (confirmed), фильтр по типам и `regime_fit`.
- **Решение** — на закрытии бара, где факт стал известен; **вход** — рыночный на **открытии следующего** бара (никогда по закрытию бара решения); если следующего бара нет — сигнал пропускается (`no_next_bar`).
- Стоп = опорный уровень невалидности (breakout — пробитый уровень, pullback — уровень ∓ зона, sweep — экстремум свипа, range — граница) ∓ `stop_buffer_atr`·ATR. Цель = `take_profit_r`·R от фактической цены входа. Размер = `risk_per_trade_quote` / |цена решения − стоп|; R считается от **фактического** входа и неизменяемого стопа (учёт Phase 0).
- Внутри бара: открытие за стопом → исполнение по открытию (гэп); открытие за целью → по цели (лимит не исполняется лучше); стоп и цель в одном баре → **стоп** (`stop_ambiguous`, помечено); выход по времени — на закрытии `max_hold_bars`-го бара; открыта в конце данных → по последнему закрытию (`end_of_data`).
- Одна позиция на символ; сигналы при открытой позиции пропускаются с причиной.
- PnL, комиссии, funding, атрибуция проскальзывания и R считаются **моделью учёта Phase 0** (`Trade`/`Fill` с `EventSource.SIMULATOR`/`FundingEvent`) — каждая издержка учитывается ровно один раз; `SimulatedTrade` запрещает net ≠ gross − fees + funding и исполнение раньше решения.
- Equity curve — mark-to-market на закрытии каждого бара (реализованный net + нереализованный результат открытых позиций − уплаченная комиссия входа); drawdown по ней.

## Cost Model
| Параметр | v1 (ДОПУЩЕНИЕ) | Применение |
|---|---|---|
| taker_fee_bps | 5.5 | рыночные входы, стопы, выходы по времени |
| maker_fee_bps | 2.0 | исполнение цели (лимит) |
| slippage_bps | 2.0 | против позиции на каждом рыночном исполнении; в цене исполнения → входит в gross, отдельно показывается как атрибуция |
| funding_mode | data_or_assumed | ставки из датасета, если есть; иначе допущение (флаг `FUNDING_ASSUMED`); никогда оба |
| assumed_funding_rate_8h | 0.0001 | в 00/08/16 UTC; long платит положительную ставку |
Нулевая модель издержек возможна только явно и помечается `ZERO_COST_MODEL`. В отчёте: gross, fees, funding, slippage (атрибуция), net.

## Метрики
Outcomes: число valid / censored / incomplete / no_anchor по горизонтам, mean и median forward return, MFE, MAE, доля достижения порогов, что достигнуто первым.
Backtest: trades, gross / fees / funding / slippage / net, win rate, profit factor, expectancy на сделку, средний и медианный R, стандартное отклонение R, max drawdown (абс. и %), серии выигрышей/убытков, exposure (доля баров в позиции), число неоднозначных и end-of-data выходов.
Любая метрика с нулевым знаменателем или без данных → `{"status": "not_available", "reason": ...}`.
Каждый backtest-отчёт содержит dataset id, replay и backtest run id, период, символы, конфиг и cost model, версии, funding source, пропущенные сигналы с причинами, data quality summary, предупреждения и ограничения.

## Train / Validation / Test
- Хронологические границы по времени датасета (доли из конфига, по умолчанию 60/20/20), без перемешивания.
- Принадлежность — по началу наблюдения (якорь / решение).
- **Purge**: наблюдение/сделка, чьё окно (горизонт / выход) доходит до конца своей выборки, исключается (метка пересекла бы в следующую).
- **Embargo**: наблюдения, начинающиеся в первые `embargo_bars` после границы, исключаются.
- Отчёты outcomes и backtest считаются отдельно по каждой выборке. Оптимизатора нет; параметры на test не подбираются; in-sample результат не является доказательством.
- Ограничение: purge работает по окну одного наблюдения; перекрытие окон *разных* наблюдений внутри одной выборки (зависимость наблюдений) не корректируется.

## Persistence (`0008_research.sql`)
`research_datasets`, `research_runs` (kind: replay / outcomes / backtest; детерминированный `run_id`; `result_hash`), `replay_observations`, `outcome_observations`, `sim_trades`, `sim_equity`, `metric_reports`, `research_conflicts`. Всё неизменяемо (триггеры). Повторный запуск с тем же результатом — no-op; с другим результатом — запись в `research_conflicts` + `StorageError`, сохранённый результат не меняется. Промежуточные snapshot replay не пишутся.

## CLI
```
python -m app research dataset build --symbols ETHUSDT BTCUSDT --tf 15m --start 2026-01-01T00:00+00:00 --end 2026-03-01T00:00+00:00 [--with-context]
python -m app research dataset show --id <ds>
python -m app research replay   --dataset <ds> [--with-regime]
python -m app research outcomes --replay <rp>
python -m app research backtest --replay <rp> [--config config/backtest_v1.json]
python -m app research run      --id <run>
python -m app research metrics  --id <run>
python -m app research export   --id <run> --out report.json
python -m app research status
```
Перед исследованием на реальных данных нужно загрузить историю (`data backfill`) и funding (`data funding`).

## Известные ограничения
- OHLCV-симуляция: нет стакана, очереди, market impact; порядок high/low внутри бара неизвестен (консервативное правило).
- Стоимость replay: около 0.1 с на бар на символ после прогрева кэша (+ режим, если включён). Месяц 15m ≈ 3000 баров ≈ 5 минут на символ.
- Funding-notional берётся по закрытию бара, заканчивающегося в момент funding (по цене входа, если бара нет).
- Количество в сделке не округляется до шага инструмента, минимальные размеры ордера не проверяются.
- Зависимость (перекрытие) наблюдений внутри выборки не корректируется; доверительные интервалы не считаются.
- Survivorship bias: датасет строится по выбранным пользователем символам; делистинги в Phase 1 не отслеживаются.
