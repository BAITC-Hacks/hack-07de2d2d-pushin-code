# Контракт · **v0.4 — черновик, не финал** · 23.09 15:25 · меняет только мастер

**Продукт:** агент диспетчера ВЭС. На каждую дату выпуска сам берёт архивный прогноз погоды,
доступный на момент выпуска, считает почасовую выработку на 48 ч с интервалом P10–P90,
анализирует результат, пересчитывает при обновлении погоды и публикует выпуск.
Задача — `docs/hackathon/task.md`. Данные — `docs/DATA_NOTES.md`.
**Как это видит пользователь — `docs/ui-mockup.html`** (кликабельный макет, данные в нём выдуманы).
Схема взаимодействий — `docs/architecture.html`.

Изменения v0.2: выпусков 29 (по 28.02 включительно) · погода сначала из API, кэш — запасной путь ·
в феврале факта нет, SCADA в признаках не используется · ленты шагов агента сохраняются · API под UI (§6) ·
экраны по макету (§9) · структура репозитория (§12).
Изменения v0.3: внутренние интерфейсы между Кубой и Абылаем (§5.1) · запись выпуска `{D}.json` · форматы времени в CSV (§7) ·
T4, T5 и фикстуры — Абылай · общие `windcast/paths.py` и `windcast/timeline.py` уже в main.
Изменения v0.4: правило «без будущего» учитывает задержку публикации прогона — `init + 8 ч ≤ T` (§2).

## 1. Время и выпуски
- Внутри — только **UTC**. В файлах и на экране — местное **UTC+5** (Asia/Almaty после 01.03.2024).
- Выпуск `D` (issue_date) = момент **T = (D+1) 00:00 местного** (= D 19:00 UTC).
- Горизонт `h = 1…48`: целевые часы (D+1) 00:00 … (D+2) 23:00 местного.
- **Тест:** D = 2026-01-31 … 2026-02-28 — **29 выпусков**. ТЗ: «повторить прогнозирование в течение
  тестового периода» — выпуск от 28.02 (на 01–02.03) закрывает оба прочтения.
- **Бэктест для метрики:** D = 2025-12-31 … 2026-01-29 (факт известен).
- **Факта за февраль нет** (данные кончаются 31.01.2026 23:50). Сравнение с фактом — только январь.

## 2. Правило «без будущего» — главная проверка задачи
- **Прогон доступен не в момент старта, а примерно через 8 ч** — ECMWF публикует данные с задержкой. Правило:
  **`init + 8 ч ≤ T`**, то есть прогон стартовал не позже D 11:00 UTC. Иначе прогон, стартовавший в D 18:00 UTC,
  формально «до T», но опубликован уже после момента выпуска. Код — `timeline.RUN_AVAILABILITY_DELAY`,
  `timeline.published_before_issue()`.
- Погода — Open-Meteo Previous Runs API, `*_previous_dayN` с **`N = timeline.lead_days(h)`**: h 1–17 → day1,
  h 18–41 → day2, h 42–48 → day3 (`run="latest"`); для v1 (`run="previous"`) — на сутки старше. `previous_day0`,
  факт и реанализ в признаках прогноза **запрещены**.
- У каждой строки прогноза хранится `weather_init_max_utc` — верхняя граница старта прогона: целевой час минус
  N суток, округлённый вниз до 00/06/12/18 UTC. `verify.py` проверяет **`weather_init_max_utc + 8 ч ≤ T`** для всех строк.
- **SCADA-признаки в прогнозе не используются** — в феврале их нет. Модель строится только на погоде.
- **Обучение — на тех же лагах:** признаки `previous_day1/day2` против фактической мощности
  (данные с ~06.2024). Модель сама учит ошибку погодной модели.

## 3. Данные и хранение
| Путь | Что | В git |
|---|---|---|
| `data/raw/turbine_{1,2}.csv` | как выдано | да |
| `data/processed/hourly.parquet` | `ts_utc, turbine, wind_ms, power, temp_c, valid` | нет (генерится) |
| `data/weather_cache/*.json` | ответы Open-Meteo | **да** — запасной путь без сети |
| `models/*.pkl` | обученные модели | да (если < 20 МБ) |
| `outputs/forecasts/{D}.csv` | выпуск, последняя версия (§7) | да, к сдаче |
| `outputs/forecasts/{D}.json` | запись выпуска со всеми версиями — источник для API (§5.1) | да |
| `outputs/metrics_jan.json` | метрики и ряды января — источник для «Качества» (§5.1) | да |
| `outputs/traces/{D}.jsonl` | ленты шагов агента по схеме событий | да, к сдаче — доказательство, что выпуски сделал агент |
| `outputs/live/` | live-выпуски | нет |

- **Погоду агент получает сам:** `fetch_weather` сначала идёт в Open-Meteo, кэш — только если сеть
  недоступна. Источник пишется в событие: `meta.source = "api" | "cache"`.
- `valid = false`: пропуски, простой (мощность < 0.02 при ветре > 5 м/с), 18.05–17.07.2024 у турбины 1.
- `SCADA_UTC_OFFSET_H` в конфиге, 5 или 6 — выбирается по бэктесту.
- **ВЭС (`plant`) = среднее нормированной мощности двух турбин.** Мощность везде — доля номинала, [0, 1].

## 4. Модель
- Признаки: ветер 100 м (и 10 м, если есть), ветер³, направление sin/cos, температура, час, месяц, `h`, турбина.
- Цель: часовая нормированная мощность. Выход **P10/P50/P90**, обрезка в [0, 1], P10 ≤ P50 ≤ P90.
- Для бэктеста января учим на данных < 2026-01-01, для февраля — на всём до 31.01.2026.
- Базовые линии (обязательны, для метрики): персистентность (последние 24 ч), климатология час×месяц,
  кривая мощности от прогнозного ветра.
- Метрики: **nMAE, nRMSE** (мощность уже нормирована = % от номинала), по горизонту и по дням; покрытие P10–P90.
- На доведение точности — не больше 40 минут. Цель — обогнать базовые линии, не рекорд.

## 5. Агент
Инструменты — детерминированные функции. Порядок и решения — за LLM (OpenAI, function calling).
Числа прогноза **считает только модель**, LLM их не придумывает и не правит.

| Инструмент | Этап ТЗ (`meta.stage`) | Вход → выход |
|---|---|---|
| `fetch_weather(issue_date, run="latest"\|"previous")` | `weather` | погода на окно, с `init`-метками, из API или кэша |
| `check_data(issue_date)` | `prep` | пропуски, свежесть прогона, прогоны ≤ T |
| `run_model(issue_date, weather)` | `model`, `forecast` | P10/P50/P90 по часам × {1, 2, plant} |
| `analyze_forecast(run_id)` | `analysis` | JSON: ветер > 20 м/с, обледенение (t ∈ [−3; +1] °C), расхождение погодных моделей, рампы ≥ 0.3 за 2 ч, скачок против прошлой версии |
| `recalc_forecast(issue_date, reason)` | `recalc` | повтор с обновлённой погодой → новая версия |
| `publish_forecast(issue_date, version, summary)` | — (**action**) | пишет CSV, ленту шагов и запись в журнал выпусков |

- **Повторный расчёт.** v1 — на прогоне на сутки старше (`lead_days(h, previous=True)`). Событие «вышел новый
  прогон» (`lead_days(h)`) будит агента: если средний сдвиг ветра > 0.5 м/с или флаг анализа —
  пересчёт → v2 с объяснением «что изменилось». Иначе — событие с `meta.status = "skip"`: «пересчёт не нужен».
- **Если более свежего прогона ≤ T нет** — агент отказывается пересчитывать и пишет почему
  («прогон вышел бы после момента выпуска»). Это показываем на демо как соблюдение правила.
- **Live:** `fetch_weather` для `issue_date = "live"` берёт Open-Meteo Forecast API (последний прогон),
  окно — 48 ч от следующего полного часа. v1 — на предыдущем прогоне, пересчёт — когда вышел новый.
- **Без `OPENAI_API_KEY`** — детерминированный режим: тот же порядок, анализ по шаблону, **те же числа**.
- Лимит 8 шагов. След — строго по `docs/references/trace-event-schema.md`; `publish_forecast` → `type: action`.
- **Кнопки в UI.** «↻ Перевыпустить агентом» (`trigger=issue`) — агент на глазах проходит весь цикл: v1 →
  видит новый прогон → решает о v2. «⚡ Новый прогон погоды» (`trigger=new_weather_run`) — агент ищет прогон
  свежее текущего и ≤ T; на архивном дне, где v2 уже учла последний прогон, честно отказывается.
- **Поля `meta`, которые читает UI:** `tool`, `stage` (`weather|prep|model|forecast|analysis|recalc`),
  `status` (`ok|warn|skip|error`), `issue_date`, `version`, `source` (`api|cache`). Свободный текст
  «почему» — в `title`, детали — в `body`.

## 5.1 Внутренние интерфейсы — чтобы ветки сливались без конфликтов
**Общие модули (мастер, уже в main):** `windcast/paths.py` — все пути от `WINDCAST_ROOT` (по умолчанию корень
репозитория); `windcast/timeline.py` — момент выпуска T, горизонт, даты выпусков, ISO-форматы. Свои пути и
часовые пояса не заводить. Зависимости — `backend/requirements.txt` (есть всё нужное, версии зафиксированы).

**Куба отдаёт** (`windcast/weather.py`, `windcast/data.py`, `windcast/model.py`):
```python
fetch_weather(issue_date: str, run: str = "latest") -> dict
# issue_date: "2026-02-13" или "live"; run: "latest" (N = timeline.lead_days(h)) | "previous" (v1: N + 1)
# {"issue_date": str, "issue_time_utc": datetime (UTC),
#  "hourly": DataFrame[h, target_time_utc, wind_100m_ms, wind_10m_ms, wind_dir_deg, temp_c, init_time_utc] — 48 строк,
#  "runs": [{"hours": "1-24", "model": "ecmwf_ifs025", "init_utc": "2026-02-13T00:00Z"}, {"hours": "25-48", ...}],
#  "source": "api" | "cache"}
check_data(issue_date: str, weather: dict) -> dict
# {"ok": bool, "missing_hours": int, "runs_before_issue": bool, "notes": [str]}
predict(issue_date: str, weather: dict) -> DataFrame
# 144 строки: h, target_time_utc, turbine ("1" | "2" | "plant"), p10, p50, p90, wind_fc_ms, temp_fc_c
MODEL_VERSION: str  # в model.py, например "lgbm-q-2026-01-31"
```
Плюс `outputs/metrics_jan.json` — ровно ответ `GET /api/metrics` (§6.6) и поле
`"series": [{"target_time_local", "turbine", "p10", "p50", "p90", "actual"}]` за весь январь.

**Абылай поверх:** `windcast/ports.py` берёт функции Кубы, а пока их нет — `windcast/_stubs.py` с тем же
форматом и `source = "stub"` (видно в `/health` и в событиях; к freeze заглушек на главном пути быть не должно).
```python
agent.run_issue(issue_date: str, *, mode: str = "agent", trigger: str = "issue",
                emit: Callable[[dict], None] = ...) -> dict
# issue_date: "2026-02-13" или "live"; события — по схеме §5; ValueError → API 400
# возвращает {"issue_date", "version", "record_path", "csv_path", "trace_path"}
```
**Запись выпуска** `outputs/forecasts/{D}.json` (live — `outputs/live/latest.json`) — единственный источник для API:
```json
{"issue_date": "2026-02-13", "issue_time_local": "2026-02-14T00:00+05:00", "issue_time_utc": "2026-02-13T19:00Z",
 "latest_version": 2, "mode": "agent", "recorded_at": "2026-09-23T15:40+05:00",
 "versions": {"1": {"version": 1, "created_at": "…", "weather_runs": [], "source": "api", "change_note": null,
                    "summary": "…", "flags": [], "rows": []},
              "2": {}}}
```
`rows` и `flags` — как в §6.2. `{D}.csv` — только последняя версия (§7). `traces/{D}.jsonl` — события последнего
полного выпуска (`trigger = "issue"`), по одному JSON на строку.

## 6. API (backend, FastAPI, порт 8000) — всё, что нужно экрану

**Общие правила.** JSON. Даты `YYYY-MM-DD`. Целевое время — ISO с `+05:00`, время прогонов — ISO с `Z`.
Мощность — доля [0, 1]. Ошибки — `400`/`404` с `{"error": "текст по-русски"}`, **не 500**.
Параметры в `?query` необязательны — без них разумное значение по умолчанию.
**Фикстуры:** Абылай (T5a) кладёт по одному JSON на каждый ответ в `frontend/fixtures/` (имя = путь,
например `forecasts_2026-02-13.json`). Сула работает против них, пока
бэкенда нет.

### 6.1 Календарь выпусков
`GET /api/issues?from=2026-01-31&to=2026-02-28`
```json
[{"issue_date": "2026-02-13", "issue_time_local": "2026-02-14T00:00+05:00",
  "status": "published | missing | running", "version": 2, "versions": [1, 2],
  "mean_p50": 0.41, "peak_p50": 0.88,
  "flags": {"ramp": 1, "ice": 1, "wind_gt20": 0, "models_diverge": 0}, "source": "api"}]
```

### 6.2 Выпуск
`GET /api/forecasts/{issue_date}?version=latest|1|2&turbine=all|plant|1|2`
```json
{"issue_date": "2026-02-13", "issue_time_local": "2026-02-14T00:00+05:00",
 "issue_time_utc": "2026-02-13T19:00Z", "version": 2, "versions": [1, 2],
 "weather_runs": [
   {"hours": "1-17",  "model": "ecmwf_ifs025", "init_utc": "2026-02-13T06:00Z", "before_issue": true},
   {"hours": "18-41", "model": "ecmwf_ifs025", "init_utc": "2026-02-12T06:00Z", "before_issue": true},
   {"hours": "42-48", "model": "ecmwf_ifs025", "init_utc": "2026-02-11T12:00Z", "before_issue": true}],
 "change_note": "ветер +0,9 м/с → пик +6 п.п. против v1",
 "summary": "Пик 88 % номинала — 14.02 13:00 …",
 "flags": [{"kind": "ramp | ice | wind_gt20 | models_diverge", "from_h": 15, "to_h": 17, "text": "спад −42 % за 2 ч"}],
 "rows": [{"h": 1, "target_time_local": "2026-02-14T00:00+05:00", "turbine": "plant",
           "p10": 0.12, "p50": 0.21, "p90": 0.33, "wind_fc_ms": 6.4, "temp_fc_c": -4.1, "actual": null}]}
```
`actual` = `null` в феврале; в январе — факт (для экрана «Качество»).
- `GET /api/forecasts/{issue_date}.csv?version=latest` — CSV выпуска по §7.
- `GET /api/forecasts/february.csv` — сводный `outputs/forecast_feb2026.csv`.

### 6.3 Запуск агента и его шаги
`POST /api/runs`
```json
{"issue_date": "2026-02-13 | live", "mode": "agent | deterministic", "trigger": "issue | new_weather_run"}
```
→ `{"id": "r_…"}`
- `trigger = "issue"` — полный выпуск: v1 → проверка обновления → возможно v2. Кнопка «↻ Перевыпустить агентом».
- `trigger = "new_weather_run"` — кнопка «⚡ Новый прогон погоды»: агент ищет прогон свежее текущего и ≤ T,
  решает сам — пересчитать, оставить или отказаться.
- `GET /api/runs/{id}/events` — SSE, `data: <событие по схеме>`. Последнее событие — `verdict`, после него
  поток закрывается.
- `GET /api/runs/{id}` → `{"id", "issue_date", "trigger", "mode", "done", "version", "events": [...]}` —
  если SSE оборвался.
- `GET /api/traces/{issue_date}?version=latest` → `{"issue_date", "version", "recorded_at", "events": [...]}` —
  сохранённая лента из `outputs/traces/{D}.jsonl`. Ею «Воспроизвести февраль» проигрывает выпуски без LLM.

### 6.4 Февраль целиком
`POST /api/backtest` `{"from": "2026-01-31", "to": "2026-02-28", "mode": "agent | deterministic"}` → `{"id"}` —
выпуски подряд, события через тот же `GET /api/runs/{id}/events`, в каждом `meta.issue_date`.
Кнопка в UI по умолчанию проигрывает **сохранённые** ленты (§6.3) и честно пишет «запись прогона от …».

### 6.5 Live
- `GET /api/live/status` →
  `{"now_local", "latest_run_utc", "next_run_utc", "next_run_available_local", "current": {"version", "issued_at_local", "weather_run_utc"} | null}`
- Выпуск — `POST /api/runs` с `"issue_date": "live"`. Результат — `GET /api/forecasts/live` (формат §6.2).

### 6.6 Качество (январь)
- `GET /api/metrics?from=2025-12-31&to=2026-01-29` →
  `{"period", "issues_count", "coverage_p10_p90", "methods": [{"key": "model | power_curve | climatology | persistence", "label", "nmae", "nrmse"}], "by_horizon": [{"h", "model", "power_curve"}]}`
- `GET /api/metrics/series?from=2026-01-15&to=2026-01-21&turbine=plant` →
  `[{"target_time_local", "p10", "p50", "p90", "actual"}]` — для графика «прогноз против факта».

### 6.7 Служебные
`GET /health` → `{"ok": true, "mode": "agent | deterministic", "model_version": "…", "issues_ready": 29}`

## 7. Формат выпуска (CSV)
`issue_date, issue_time_local, target_time_local, horizon_h, turbine, p10, p50, p90, wind_fc_ms, weather_init_max_utc, version`
`turbine ∈ {1, 2, plant}`. 48 × 3 строк на выпуск. Плюс сводный `outputs/forecast_feb2026.csv` (29 выпусков).
Время: `issue_time_local`, `target_time_local` — `2026-02-14T00:00+05:00`; `weather_init_max_utc` — `2026-02-13T00:00Z`.
`target_time_local` — начало часа: h = 1 → (D+1) 00:00. Мощность — 4 знака после запятой.

## 8. CLI — для эксперта, без UI
Запуск из корня репозитория, с `PYTHONPATH=backend` (или внутри контейнера):
```
python -m windcast.backtest --from 2026-01-31 --to 2026-02-28   # 29 выпусков → outputs/forecasts/ + outputs/traces/
python -m windcast.evaluate --from 2025-12-31 --to 2026-01-29   # метрики января
python scripts/verify.py                                        # PASS/FAIL по проверкам
```

## 9. Экраны (frontend, порт 3000, API по `/api/...`) — по макету `docs/ui-mockup.html`
Открывается сразу рабочий экран, без обложки. Февраль уже посчитан. Тёмная тема «диспетчерская».

**Шапка:** Windcast · координаты · вкладки **Февраль 2026 · тест** / **Live · сейчас** / **Качество** ·
режим агента (`/health.mode`) · кнопка **«Как проверить за 3 минуты»**.

1. **Февраль 2026 · тест**
   - **Машина времени:** «Для системы сейчас 13.02.2026 24:00 — всё правее она не видела».
   - **Календарь 31.01 → 28.02** (`/api/issues`): высота столбика — `mean_p50`, точка — есть флаги, `v2` — был пересчёт.
   - **«▶ Воспроизвести февраль»** — проигрывает выпуски по дням из `/api/traces/{D}`, подпись «запись прогона от …».
   - **График 48 ч** (`/api/forecasts/{D}`): P50, коридор P10–P90, прошлая версия пунктиром, разделитель суток,
     флаги метками по оси времени, ветер под графиком с линиями «пуск 3 м/с» и «номинал 11,5 м/с», подсказка по часу.
     Переключатель **ВЭС / Т1 / Т2**.
   - **«✓ Без будущего»** — раскрывает `weather_runs`: какой прогон, когда вышел, что до момента выпуска.
   - **Кнопки:** «⬇ CSV выпуска», «↻ Перевыпустить агентом» (`trigger=issue`), «⚡ Новый прогон погоды» (`trigger=new_weather_run`).
   - **Панель агента справа:** шесть этапов ТЗ (погода · подготовка · модель · прогноз · анализ · пересчёт),
     загораются по `meta.stage`; лента шагов по SSE; «Сводка для диспетчера» (`summary`).
2. **Live · сейчас** (`/api/live/status`): текущее время, последний и следующий прогон; «▶ Выпустить прогноз сейчас»,
   «Проверить обновления погоды»; тот же график и панель агента.
3. **Качество** (`/api/metrics`, `/api/metrics/series`): четыре числа (nMAE, выигрыш против лучшей базовой,
   покрытие P10–P90, число выпусков), модель против трёх базовых, ошибка по горизонту, неделя «прогноз против факта».
   Текст: «факта за февраль у нас нет — CSV готовы для сравнения организатором».
4. **«Как проверить за 3 минуты»** — боковая панель: требование ТЗ → где видно → кнопка «Показать»
   (переключает вкладку и подсвечивает элемент).

## 10. Сквозной сценарий демо (3 минуты)
1. Открываем `https://pushin.codes` — февраль уже посчитан, в календаре 29 выпусков.
2. «▶ Воспроизвести февраль» (10 с) → кликаем 13.02 → «✓ Без будущего».
3. На дне с v2 — «↻ Перевыпустить агентом»: агент на глазах делает v1, видит новый прогон, пересчитывает → v2,
   v1 уходит в пунктир. Затем «⚡ Новый прогон погоды» — агент отказывается: свежий прогон был бы из будущего.
4. Live → «▶ Выпустить прогноз сейчас» — прогноз от текущего момента.
5. «Качество» — модель обгоняет базовые линии на январе.

## 11. Владение и стек
- **Куба:** `windcast/config.py`, `data.py`, `weather.py`, `model.py`, `evaluate.py`, их тесты; `data/`, `models/`,
  `outputs/metrics_jan.json`.
- **Абылай:** `windcast/ports.py`, `_stubs.py`, `tools.py`, `agent.py`, `store.py`, `api.py`, `backtest.py`, их тесты;
  `backend/Dockerfile`; `frontend/fixtures/`; `outputs/forecasts/`, `outputs/traces/`.
- Общие (мастер): `windcast/__init__.py`, `paths.py`, `timeline.py`, `backend/requirements*.txt`, `backend/pyproject.toml`.
  Нужна новая зависимость — одна строка в конец `requirements.txt` с `==`.
- `frontend/` (стек на выбор; график — ECharts или аналог с полосой; SSE — `EventSource`) — **Сула**.
- `docs/`, `README.md`, `scripts/verify.py`, `infra/`, корневой `docker-compose.yml` — **мастер**.
- Версии зависимостей фиксировать (`requirements.txt` с `==`, lock-файл фронта). Секреты — только `.env`.

## 12. Структура репозитория
```
backend/            Куба · FastAPI + пакет windcast, порт 8000, Dockerfile собирается из корня
  windcast/         paths · timeline (общие) · config · data · weather · model · evaluate (Куба) ·
                    ports · _stubs · tools · agent · store · api · backtest (Абылай)
  tests/
frontend/           Сула · экраны §9, порт 3000
  fixtures/         JSON-ответы §6 до готовности бэкенда
data/raw/           исходные CSV, не трогать
data/processed/     hourly.parquet — генерится, не в git
data/weather_cache/ ответы Open-Meteo — в git
models/             обученные модели
outputs/forecasts/  29 CSV выпусков — к сдаче
outputs/traces/     29 лент шагов агента — к сдаче
scripts/verify.py   мастер · проверка «без будущего», формата, диапазонов
infra/              мастер · compose для сервера, Caddy, деплой
docs/               контракт, макет, схема, данные, рубрика
```
В каждой директории `README.md`: что лежит и кто владелец.
