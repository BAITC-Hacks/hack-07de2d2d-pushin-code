# data — Куба

- `raw/` — CSV турбин как выданы организатором. **Не менять** (SHA-256 в `docs/DATA_NOTES.md`).
- `processed/` — `hourly.parquet`, генерится `windcast.data`. В git не попадает.
- `weather_cache/` — ответы Open-Meteo. В git: запасной путь, если на проверке нет сети.
  Основной путь — агент сам ходит в API (`meta.source = "api"`).
