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
