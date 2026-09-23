# frontend — Сула

Экраны по `docs/CONTRACT.md` §9 и макету `docs/ui-mockup.html` (открыть в браузере; данные в макете выдуманы).
Порт 3000. API — только по относительному пути `/api/...` (на сервере проксирует Caddy, локально — dev-proxy).

- `fixtures/` — JSON-ответы §6, по одному на эндпоинт (имя = путь: `issues.json`, `forecasts_2026-02-13.json`,
  `traces_2026-02-13.json`, `metrics.json`, `live_status.json`). Пока бэкенда нет — работать против них.
- Шаги агента — `EventSource` на `/api/runs/{id}/events`, событие по `docs/references/trace-event-schema.md`;
  этапы ТЗ подсвечиваются по `meta.stage`.
- `Dockerfile`: слушает `0.0.0.0:3000`. Lock-файл в git, версии зафиксированы.
