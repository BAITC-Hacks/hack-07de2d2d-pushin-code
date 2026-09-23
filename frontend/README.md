# frontend — Сула

Экраны по `docs/CONTRACT.md` §9. React 18 + Vite, без UI-библиотек: графики — свой SVG
(`src/ForecastChart.jsx`), шаги агента — `EventSource` на `/api/runs/{id}/events` с запасным
опросом `GET /api/runs/{id}`, если поток оборвался. Светлая тема. Порт 3000. API — только по относительному пути
`/api/...` и `/health` (в compose проксирует Caddy, в `npm run dev` — dev-proxy Vite на `localhost:8000`).

| Файл | Что |
|---|---|
| `src/App.jsx` | шапка, вкладки (`#february`, `#live`, `#quality`), режим агента из `/health`, панель «Как проверить за 3 минуты» |
| `src/February.jsx` | машина времени, календарь 29 выпусков, «Воспроизвести февраль», график, ВЭС / Т1 / Т2, риски, «Без будущего», CSV, «Перевыпустить агентом», «Новый прогон погоды» |
| `src/Live.jsx` | статус прогонов, «Выпустить прогноз сейчас», «Проверить обновления погоды», «Имитировать сбой погоды», журнал агента |
| `src/Quality.jsx` | январь: nMAE, выигрыш против лучшей базовой линии, покрытие P10–P90, ошибка по горизонту, неделя «прогноз против факта» |
| `src/AgentPanel.jsx` | шесть этапов по `meta.stage`, лента событий, сводка для диспетчера |
| `src/useAgentRun.js` | запуск агента (POST + SSE) и проигрывание сохранённых лент |
| `src/api.js` | клиент API §6 и разбор ответов |

## Запуск

```bash
npm ci
npm run dev        # http://localhost:3000, API проксируется на localhost:8000
npm test           # vitest
npm run build      # то же делает Dockerfile
```

`?fixtures=1` в адресе — работа без бэкенда на JSON из `fixtures/` (данные синтетические, только для разработки).
В обычном режиме фикстуры не загружаются.

**Образ:** `Dockerfile` собирает приложение (`npm ci && npm run build`) и отдаёт `dist/` через nginx на `0.0.0.0:3000`;
`nginx.conf` проксирует `/api/*` и `/health` на `backend:8000` (SSE без буферизации). Из корня `docker compose up --build` →
http://localhost:8080 (Caddy) или http://localhost:3000 (nginx сам проксирует API). Lock-файл в git.
