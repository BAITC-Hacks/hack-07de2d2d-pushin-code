# frontend — Сула

Экраны по `docs/CONTRACT.md` §9 и макету `docs/ui-mockup.html` (открыть в браузере; данные в макете выдуманы).
Порт 3000. API — только по относительному пути `/api/...` (nginx этого контейнера проксирует `/api/` и `/health` на `backend:8000`).

- `fixtures/` — JSON-ответы §6, по одному на эндпоинт (имя = путь: `issues.json`, `forecasts_2026-02-13.json`,
  `traces_2026-02-13.json`, `metrics.json`, `live_status.json`). Пока бэкенда нет — работать против них.
- Шаги агента — `EventSource` на `/api/runs/{id}/events`, событие по `docs/references/trace-event-schema.md`;
  этапы ТЗ подсвечиваются по `meta.stage`.
- `Dockerfile`: `nginx:1.27-alpine`, слушает `0.0.0.0:3000`. Сборки нет: чистые HTML/JS, сторонних JS-библиотек нет.

## Запуск

Без сборки и npm: `index.html` + `app.js` (vanilla JS, SVG-графики), nginx отдаёт статику на `0.0.0.0:3000`
и проксирует `/api/*` и `/health` на `backend:8000` (`nginx.conf`, SSE без буферизации).

- Вся система: из корня `docker compose up --build` → http://localhost:8080 (Caddy) или http://localhost:3000 (nginx сам проксирует API).
- Только фронт: `docker build -t windcast-frontend frontend/ && docker run --rm -p 3000:3000 windcast-frontend`
  (без контейнера `backend` в той же сети запросы к `/api/*` вернут 502 — страница покажет «API недоступен»).
- Вкладки открываются по хэшу: `/#feb`, `/#live`, `/#q`.
- Данные — только из API: `/api/issues`, `/api/forecasts/{D}` (+`?version=N` для пунктира прошлой версии),
  `/api/traces/{D}`, `POST /api/runs` → `EventSource` на `/api/runs/{id}/events`, `/api/live/*`, `/api/metrics*`, `/health`.
