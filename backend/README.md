# backend — Куба

FastAPI + пакет `windcast`. Порт 8000. Контракт — `docs/CONTRACT.md` §5, §6, §8.

- `windcast/` — модули: `config` (пути, `SCADA_UTC_OFFSET_H`, пороги) · `data` (T1: raw → `data/processed/hourly.parquet`) ·
  `weather` (T2: Open-Meteo Previous Runs / Forecast, кэш в `data/weather_cache/`) · `model` (T3: P10/P50/P90, базовые линии) ·
  `tools` (6 инструментов, один реестр) · `agent` (LLM-цикл + детерминированный режим) · `api` (FastAPI, SSE) ·
  `backtest`, `evaluate` (CLI) · `live`.
- `tests/` — pytest.
- Пути к `data/`, `models/`, `outputs/` считаются от корня репозитория, а не от `backend/`.
- `Dockerfile` собирается с контекстом **корня** репозитория (нужны `data/` и `models/`): в compose
  `build: {context: ., dockerfile: backend/Dockerfile}`. Слушает `0.0.0.0:8000`, `GET /health`.
- `requirements.txt` — версии через `==`. Ключ OpenAI — только из окружения; без ключа — детерминированный режим.

## T1 · почасовой SCADA

`python -m windcast.data` читает два исходных CSV и создаёт некоммитимый
`data/processed/hourly.parquet` со схемой `ts_utc, turbine, wind_ms, power, temp_c, valid`.
Команда печатает реальные часы и долю `valid` по каждой турбине. Неполный час, простой
(`power < 0.02` при `wind_ms > 5`) и период T1 18.05–17.07.2024 получают `valid=false`.
Смещение местного времени SCADA задаётся `SCADA_UTC_OFFSET_H=5` или `6`; по умолчанию 5,
а T3 выбирает кандидат по январскому бэктесту.

## T2 · погода

`windcast.weather.fetch_weather` всегда сначала обращается к Open-Meteo Previous Runs, затем использует только валидный кэш. Для каждого часа хранится консервативная верхняя граница инициализации (`init_time_utc`), а `best_match` честно обозначен как модель источника.
