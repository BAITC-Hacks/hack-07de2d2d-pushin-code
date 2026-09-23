# Оценка февраля / February evaluation

## Русский

**Файлы** (собираются `python3 scripts/export_evaluation.py` из `outputs/forecast_feb2026.csv` и `outputs/metrics_jan.json`):

| Файл | Что | Строк |
|---|---|---|
| `february_day_ahead.csv` | один прогноз на каждый час 01.02 00:00 … 28.02 23:00 (UTC+5) × турбина {1, 2, plant}: из выпуска D−1, горизонт 1–24 | 2016 |
| `february_all_horizons.csv` | все строки всех 29 выпусков, горизонты 1–48 (для оценки второго дня), + `version` | 4176 |
| `january_day_ahead.csv` | январский бэктест в том же формате **с колонкой `actual`** — чтобы проверить сам скрипт оценки | 2201 |

Колонки: `target_time_utc` (ISO, `Z`), `target_time_local` (`+05:00`), `turbine`, `p10`, `p50`, `p90`, `issue_date`, `horizon_h`.

- **Единицы:** доля номинальной мощности 0–1. `plant` = среднее турбин 1 и 2.
- **Время:** целевой час — час, *начинающийся* в `target_time_local` (UTC+5). Выпуск D сделан в момент T = D 19:00 UTC; h = 1 — это (D+1) 00:00 UTC+5.
- **Часы SCADA организатора:** по нашему январскому бэктесту метки времени в `turbine_*.csv` — это UTC+6 (сдвиг 6 выбран по nMAE на 1–7 января). Если это не так — `--scada-utc-offset 5`.
- **Без будущего:** каждый прогноз использовал только прогоны погоды, опубликованные не позже чем за 8 ч до момента выпуска. Проверка: `python3 scripts/verify.py`.

**Оценка одной командой** (только стандартная библиотека, Python ≥ 3.9):

```bash
python3 scripts/score.py --actual <папка с turbine_1.csv и turbine_2.csv за февраль>
python3 scripts/score.py --actual <папка> --forecast outputs/evaluation/february_all_horizons.csv   # + разбивка h1–24 / h25–48
python3 scripts/score.py --actual <папка> --json                                                   # для машин
```

`--actual` принимает файлы в формате организатора (10-минутные строки, русские заголовки, CRLF/BOM) или
tidy-CSV с колонками `target_time_utc` или `target_time_local`, `turbine`, `actual`.
Агрегация SCADA: время → UTC (минус сдвиг) → среднее за час; час засчитывается при ≥ 4 из 6
десятиминутных строк (`--min-samples`); час отбрасывается как простой, если средняя мощность < 0,02
при среднем ветре > 5 м/с (то же правило, что в `backend/windcast/data.py`). `plant` — только часы,
где засчитаны обе турбины. Число отброшенных часов печатается.

**Метрики** (по p50, в долях номинала): nMAE = mean|p50 − факт|; nRMSE = √mean(p50 − факт)²;
bias = mean(p50 − факт); покрытие P10–P90 = доля часов с p10 ≤ факт ≤ p90 (цель ≈ 80 %);
pinball = среднее по q ∈ {0,1; 0,5; 0,9} от max(q·e, (q−1)·e), e = факт − p_q. Главная цифра — `plant`.

**Январь для сравнения** (проверка скрипта):

```bash
python3 scripts/score.py --actual data/raw --forecast outputs/evaluation/january_day_ahead.csv
python3 scripts/score.py --actual outputs/evaluation/january_day_ahead.csv --forecast outputs/evaluation/january_day_ahead.csv
```

Обе команды дают nMAE ВЭС **14,71 %** по 732 часам (из сырых SCADA и из колонки `actual` — одно и то же
число, 0,147123). В `outputs/metrics_jan.json` и README опубликовано **15,76 %** — это другая выборка:
все 30 выпусков × 48 ч (1417 часов, горизонты 1–48, один час оценивается до двух раз). Здесь — один
прогноз на час с наименьшим горизонтом (h 1–24; для 31.01 — h 25–47), поэтому ошибка ниже на ~1 п.п.

## English

**Files:** `february_day_ahead.csv` — one forecast per hour, 2026-02-01 00:00 … 02-28 23:00 UTC+5 ×
turbine {1, 2, plant}, from issue D−1 (horizon 1–24), 2016 rows. `february_all_horizons.csv` — every row
of all 29 issues, horizons 1–48, 4176 rows. `january_day_ahead.csv` — the January backtest in the same
format with an `actual` column, to check the scorer.

- **Units:** share of rated power, 0–1. `plant` = mean of turbines 1 and 2.
- **Time:** the target hour is the hour *starting* at `target_time_local` (UTC+5). Issue D is made at D 19:00 UTC; h = 1 is (D+1) 00:00 UTC+5.
- **Organizer SCADA clock:** our January backtest found `turbine_*.csv` timestamps are UTC+6. If yours differ, pass `--scada-utc-offset 5`.
- **No future data:** every forecast used only weather runs published ≥ 8 h before its issue moment. Check with `python3 scripts/verify.py`.

**Score in one command:** `python3 scripts/score.py --actual <folder with turbine_1.csv and turbine_2.csv>`
(add `--forecast outputs/evaluation/february_all_horizons.csv` for the h1–24 / h25–48 split, `--json` for
machines). SCADA hours need ≥ 4 of 6 ten-minute rows; hours with mean power < 0.02 while mean wind > 5 m/s
are dropped as downtime; `plant` uses hours where both turbines count. A tidy CSV
(`target_time_utc` or `target_time_local`, `turbine`, `actual`) also works.

**Metrics** on p50, share of rated: nMAE, nRMSE, bias (p50 − actual), P10–P90 coverage, mean pinball
loss over q = 0.1/0.5/0.9. Headline: `plant`.

**January reference:** both commands above give plant nMAE **14.71 %** over 732 hours (raw SCADA and the
`actual` column agree exactly). The published **15.76 %** (`metrics_jan.json`) scores all 30 issues × 48 h
(1417 hours, horizons 1–48); this file keeps one lowest-horizon forecast per hour, hence ~1 p.p. lower.
