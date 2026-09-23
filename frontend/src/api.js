// Lazy: fixtures are split into their own chunks and never load unless ?fixtures=1.
const FIXTURE_MODULES = import.meta.glob('../fixtures/*.json', { import: 'default' });

const DEFAULT_FROM = '2026-01-31';
const DEFAULT_TO = '2026-02-28';

export const RUN_EVENT_FIXTURES = Object.freeze({
  issue: 'run_events_issue.json',
  new_weather_run: 'run_events_new_weather_run.json',
});

export const WEATHER_RUN_PUBLICATION_DELAY_MS = 8 * 60 * 60 * 1000;

export class ApiError extends Error {
  constructor(message, status = 0, details = undefined) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.details = details;
  }
}

export function getFixtureMode(search = typeof window === 'undefined' ? '' : window.location.search) {
  const params = new URLSearchParams(search);
  return params.get('fixtures') === '1' || import.meta.env?.VITE_USE_FIXTURES === 'true';
}

export function fixtureNamesForPath(path) {
  const cleanPath = path.split('?')[0];
  if (cleanPath === '/api/issues') return 'issues.json';
  const forecastMatch = cleanPath.match(/^\/api\/forecasts\/(\d{4}-\d{2}-\d{2})$/);
  if (cleanPath === '/api/forecasts/live') return 'forecasts_live.json';
  if (forecastMatch) {
    const version = new URLSearchParams(path.split('?')[1] || '').get('version');
    return version === '1'
      ? `forecasts_${forecastMatch[1]}_v1.json`
      : `forecasts_${forecastMatch[1]}.json`;
  }
  const traceMatch = cleanPath.match(/^\/api\/traces\/(\d{4}-\d{2}-\d{2})$/);
  if (traceMatch) return `traces_${traceMatch[1]}.json`;
  if (cleanPath === '/api/runs/fixture/events') {
    const trigger = new URLSearchParams(path.split('?')[1] || '').get('trigger') || 'issue';
    return RUN_EVENT_FIXTURES[trigger] || null;
  }
  if (cleanPath === '/api/metrics') return 'metrics.json';
  if (cleanPath === '/api/metrics/series') return 'metrics_series.json';
  if (cleanPath === '/api/live/status') return 'live_status.json';
  if (cleanPath === '/api/live/journal') return 'live_journal.json';
  if (cleanPath === '/health') return 'health.json';
  return null;
}

async function loadFixture(name) {
  if (name === 'live_journal.json') return [];
  const match = Object.entries(FIXTURE_MODULES).find(([path]) => path.endsWith(`/${name}`));
  if (!match) {
    throw new ApiError(`Фикстура ${name} не найдена в frontend/fixtures`, 404);
  }
  return match[1]();
}

async function readError(response) {
  try {
    const body = await response.json();
    return body?.error || `Ошибка API (${response.status})`;
  } catch {
    return `Ошибка API (${response.status})`;
  }
}

function asNumber(value) {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

export function clamp(value, min = 0, max = 1) {
  return Math.max(min, Math.min(max, value));
}

export function normalizeTurbine(value) {
  if (value === 't1' || value === '1' || value === 1) return '1';
  if (value === 't2' || value === '2' || value === 2) return '2';
  return 'plant';
}

function normalizeRowTurbine(value) {
  if (value === '1' || value === 1) return '1';
  if (value === '2' || value === 2) return '2';
  if (value === 'plant') return 'plant';
  return null;
}

function rowMatchesTurbine(row, turbine) {
  const rowTurbine = normalizeRowTurbine(row?.turbine);
  return rowTurbine !== null && rowTurbine === normalizeTurbine(turbine);
}

export function parseRunEvents(payload) {
  const list = Array.isArray(payload) ? payload : payload?.events;
  if (!Array.isArray(list)) throw new ApiError('Ответ потока агента имеет неверный формат');
  return list
    .filter((event) => event && Number.isFinite(Number(event.seq)))
    .map((event) => ({
      ...event,
      seq: Number(event.seq),
      meta: event.meta && typeof event.meta === 'object' ? event.meta : {},
    }))
    .sort((left, right) => left.seq - right.seq);
}

export function weatherRunIsBeforeIssue(run, issueTime) {
  const initTime = Date.parse(run?.init_utc);
  const issueTimestamp = typeof issueTime === 'number' ? issueTime : Date.parse(issueTime);
  return run?.before_issue === true
    && Number.isFinite(initTime)
    && Number.isFinite(issueTimestamp)
    && initTime + WEATHER_RUN_PUBLICATION_DELAY_MS <= issueTimestamp;
}

function abortError() {
  const error = new Error('Поток агента остановлен');
  error.name = 'AbortError';
  return error;
}

function waitForReplayDelay(delayMs, signal) {
  if (!delayMs) {
    if (signal?.aborted) return Promise.reject(abortError());
    return Promise.resolve();
  }
  return new Promise((resolve, reject) => {
    let timer;
    const onAbort = () => {
      clearTimeout(timer);
      reject(abortError());
    };
    if (signal?.aborted) {
      onAbort();
      return;
    }
    timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, delayMs);
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

export async function replayRunEvents(payload, onEvent, { delayMs = 600, signal } = {}) {
  const events = parseRunEvents(payload);
  const delivered = [];
  for (const [index, event] of events.entries()) {
    if (index > 0) await waitForReplayDelay(delayMs, signal);
    if (signal?.aborted) throw abortError();
    await onEvent(event);
    delivered.push(event);
    if (event.type === 'verdict') {
      return { closed: true, events: delivered, verdict: event };
    }
  }
  return { closed: false, events: delivered, verdict: null };
}

function normalizeFlag(flag) {
  if (!flag || typeof flag !== 'object') return null;
  const kind = String(flag.kind || '').trim();
  if (!kind) return null;
  return {
    ...flag,
    kind,
    from_h: asNumber(flag.from_h),
    to_h: asNumber(flag.to_h),
  };
}

export function parseIssues(payload) {
  const list = Array.isArray(payload) ? payload : payload?.issues;
  if (!Array.isArray(list)) throw new ApiError('Ответ календаря имеет неверный формат');
  return list
    .filter((item) => item && /^\d{4}-\d{2}-\d{2}$/.test(String(item.issue_date)))
    .map((item) => ({
      ...item,
      issue_date: String(item.issue_date),
      version: asNumber(item.version) || 1,
      versions: Array.isArray(item.versions) ? item.versions.map(Number).filter(Number.isFinite) : [],
      mean_p50: asNumber(item.mean_p50),
      peak_p50: asNumber(item.peak_p50),
      flags: item.flags && typeof item.flags === 'object' ? item.flags : {},
      source: item.source || null,
    }))
    .sort((left, right) => left.issue_date.localeCompare(right.issue_date));
}

export function parseForecast(payload, turbine = 'plant') {
  if (!payload || typeof payload !== 'object') throw new ApiError('Ответ прогноза имеет неверный формат');
  const rows = Array.isArray(payload.rows) ? payload.rows : [];
  const selected = rows.filter((row) => rowMatchesTurbine(row, turbine));
  const normalizedRows = selected
    .map((row) => {
      const p10 = asNumber(row.p10);
      const p50 = asNumber(row.p50);
      const p90 = asNumber(row.p90);
      const h = asNumber(row.h);
      if (h === null || p10 === null || p50 === null || p90 === null) return null;
      return {
        ...row,
        h,
        turbine: normalizeRowTurbine(row.turbine),
        target_time_local: row.target_time_local || null,
        p10: clamp(p10),
        p50: clamp(p50),
        p90: clamp(p90),
        wind_fc_ms: asNumber(row.wind_fc_ms),
        temp_fc_c: asNumber(row.temp_fc_c),
        actual: row.actual === null || row.actual === undefined ? null : asNumber(row.actual),
      };
    })
    .filter(Boolean)
    .sort((left, right) => left.h - right.h);

  return {
    ...payload,
    issue_date: String(payload.issue_date || ''),
    version: asNumber(payload.version) || 1,
    versions: Array.isArray(payload.versions) ? payload.versions.map(Number).filter(Number.isFinite) : [],
    rows: normalizedRows,
    weather_runs: Array.isArray(payload.weather_runs) ? payload.weather_runs : [],
    flags: Array.isArray(payload.flags) ? payload.flags.map(normalizeFlag).filter(Boolean) : [],
    source: payload.source || null,
  };
}

export function parseLiveStatus(payload) {
  if (!payload || typeof payload !== 'object') throw new ApiError('Ответ Live имеет неверный формат');
  return { ...payload, current: payload.current && typeof payload.current === 'object' ? payload.current : null };
}

export function parseMetrics(payload) {
  if (!payload || typeof payload !== 'object') throw new ApiError('Ответ метрик имеет неверный формат');
  return {
    ...payload,
    issues_count: asNumber(payload.issues_count),
    coverage_p10_p90: asNumber(payload.coverage_p10_p90),
    methods: Array.isArray(payload.methods) ? payload.methods.map((method) => ({ ...method, nmae: asNumber(method.nmae), nrmse: asNumber(method.nrmse) })).filter((method) => method.key) : [],
    by_horizon: Array.isArray(payload.by_horizon) ? payload.by_horizon.map((item) => ({ ...item, h: asNumber(item.h), model: asNumber(item.model), power_curve: asNumber(item.power_curve) })).filter((item) => item.h !== null) : [],
  };
}

export function formatRowsForChart(forecast) {
  const rows = forecast?.rows || [];
  return {
    rows,
    p10: rows.map((row) => row.p10),
    p50: rows.map((row) => row.p50),
    p90: rows.map((row) => row.p90),
    wind: rows.map((row) => row.wind_fc_ms),
    temp: rows.map((row) => row.temp_fc_c),
    actual: rows.map((row) => row.actual),
    times: rows.map((row) => row.target_time_local),
  };
}

export function createApi({ fetchImpl = globalThis.fetch, fixtureMode = false } = {}) {
  if (!fixtureMode && typeof fetchImpl !== 'function') {
    throw new ApiError('В браузере недоступен fetch');
  }

  async function request(path, { signal } = {}) {
    if (fixtureMode) {
      const fixtureName = fixtureNamesForPath(path);
      if (!fixtureName) throw new ApiError(`Для ${path} нет явной фикстуры`, 404);
      try {
        return await loadFixture(fixtureName);
      } catch (error) {
        // A single-version issue has no _v1 file: its primary file is already v1.
        // Releases with versions [1, 2] keep the explicit _v1 lookup strict.
        if (fixtureName.endsWith('_v1.json')) {
          const primaryName = fixtureName.replace('_v1.json', '.json');
          try {
            const primary = await loadFixture(primaryName);
            if (Number(primary?.version) === 1) return primary;
          } catch {
            // Preserve the original missing-v1 error below.
          }
        }
        throw error;
      }
    }

    const response = await fetchImpl(path, { headers: { Accept: 'application/json' }, signal });
    if (!response.ok) throw new ApiError(await readError(response), response.status);
    try {
      return await response.json();
    } catch {
      throw new ApiError('API вернул не JSON', response.status);
    }
  }

  return {
    fixtureMode,
    async getIssues({ from = DEFAULT_FROM, to = DEFAULT_TO, signal } = {}) {
      const payload = await request(`/api/issues?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`, { signal });
      return parseIssues(payload);
    },
    async getForecast(issueDate, turbine = 'plant', version = 'latest', { signal } = {}) {
      const normalizedTurbine = normalizeTurbine(turbine);
      const turbineParam = normalizedTurbine === 'plant' ? 'plant' : normalizedTurbine;
      const payload = await request(`/api/forecasts/${encodeURIComponent(issueDate)}?version=${encodeURIComponent(version)}&turbine=${turbineParam}`, { signal });
      return parseForecast(payload, normalizedTurbine);
    },
    async getLiveStatus({ signal } = {}) {
      return parseLiveStatus(await request('/api/live/status', { signal }));
    },
    async getHealth({ signal } = {}) {
      return request('/health', { signal });
    },
    async getLiveJournal({ limit = 20, signal } = {}) {
      const payload = await request(`/api/live/journal?limit=${encodeURIComponent(limit)}`, { signal });
      if (!Array.isArray(payload)) throw new ApiError('Ответ журнала Live имеет неверный формат');
      return payload;
    },
    async getMetrics({ from = '2025-12-31', to = '2026-01-29', signal } = {}) {
      return parseMetrics(await request(`/api/metrics?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`, { signal }));
    },
    async getMetricsSeries({ from = '2026-01-15', to = '2026-01-21', turbine = 'plant', signal } = {}) {
      const payload = await request(`/api/metrics/series?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}&turbine=${encodeURIComponent(turbine)}`, { signal });
      if (!Array.isArray(payload)) throw new ApiError('Ответ ряда метрик имеет неверный формат');
      return payload;
    },
    async getTrace(issueDate, { signal } = {}) {
      const payload = await request(`/api/traces/${encodeURIComponent(issueDate)}?version=latest`, { signal });
      const events = parseRunEvents(payload);
      return { ...payload, events };
    },
    async getRun(runId, { signal } = {}) {
      const payload = await request(`/api/runs/${encodeURIComponent(runId)}`, { signal });
      if (!payload || typeof payload !== 'object') throw new ApiError('Ответ запуска агента имеет неверный формат');
      return { ...payload, events: parseRunEvents(payload) };
    },
    async loadForecastSelection(issueDate, turbine = 'plant', { signal } = {}) {
      const current = await this.getForecast(issueDate, turbine, 'latest', { signal });
      let previousPayload = current.previous || current.previous_version || null;
      let previousError = null;
      if (!previousPayload && current.version > 1) {
        try {
          previousPayload = await this.getForecast(issueDate, turbine, current.version - 1, { signal });
        } catch (error) {
          if (error?.name === 'AbortError') throw error;
          previousError = error;
        }
      }
      const previous = previousPayload ? parseForecast(previousPayload, turbine) : null;
      return { current, previous, previousError };
    },
    async getRunEvents(trigger = 'issue', { signal } = {}) {
      if (!fixtureMode) throw new ApiError('Поток фикстур доступен только в явном режиме fixtures');
      const normalizedTrigger = RUN_EVENT_FIXTURES[trigger] ? trigger : 'issue';
      const payload = await request(`/api/runs/fixture/events?trigger=${encodeURIComponent(normalizedTrigger)}`, { signal });
      return parseRunEvents(payload);
    },
    async replayRun(trigger = 'issue', onEvent = () => {}, options = {}) {
      const events = await this.getRunEvents(trigger, options);
      return replayRunEvents(events, onEvent, options);
    },
    async startRun(issueDate, trigger = 'issue', { signal, scenario = null } = {}) {
      if (fixtureMode) {
        return {
          id: `fixture-${issueDate}-${trigger}`,
          issue_date: issueDate,
          trigger,
          scenario,
          events: await this.getRunEvents(trigger, { signal }),
        };
      }
      const response = await fetchImpl('/api/runs', {
        method: 'POST',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
        body: JSON.stringify({ issue_date: issueDate, mode: 'agent', trigger, scenario }),
        signal,
      });
      if (!response.ok) throw new ApiError(await readError(response), response.status);
      try {
        return await response.json();
      } catch {
        throw new ApiError('API вернул не JSON', response.status);
      }
    },
    streamRun(runId, onEvent, { signal } = {}) {
      return streamRunEvents(runId, onEvent, { signal, getRun: (id) => this.getRun(id) });
    },
    csvUrl(issueDate, version = 'latest') {
      return `/api/forecasts/${encodeURIComponent(issueDate)}.csv?version=${encodeURIComponent(version)}`;
    },
    summaryCsvUrl() {
      return '/api/forecasts/february.csv';
    },
  };
}

// SSE with a polling fallback: if the stream drops, GET /api/runs/{id} still has every event.
export function streamRunEvents(runId, onEvent, { signal, getRun, EventSourceImpl = globalThis.EventSource } = {}) {
  return new Promise((resolve, reject) => {
    const seen = new Set();
    let finished = false;
    let source = null;
    const deliver = (event) => {
      if (!event || seen.has(event.seq)) return;
      seen.add(event.seq);
      onEvent(event);
    };
    const finish = (verdict) => {
      if (finished) return;
      finished = true;
      source?.close();
      resolve({ verdict });
    };
    const poll = async () => {
      try {
        for (let attempt = 0; attempt < 120 && !finished; attempt += 1) {
          if (signal?.aborted) throw abortError();
          const run = await getRun(runId);
          run.events.forEach(deliver);
          const verdict = run.events.find((event) => event.type === 'verdict');
          if (run.done || verdict) return finish(verdict || null);
          await waitForReplayDelay(1000, signal);
        }
        if (!finished) reject(new ApiError('Агент не закончил за 2 минуты'));
      } catch (error) {
        if (!finished) reject(error);
      }
    };
    signal?.addEventListener('abort', () => {
      source?.close();
      if (!finished) {
        finished = true;
        reject(abortError());
      }
    }, { once: true });
    if (typeof EventSourceImpl !== 'function') {
      poll();
      return;
    }
    source = new EventSourceImpl(`/api/runs/${encodeURIComponent(runId)}/events`);
    source.onmessage = (message) => {
      let event;
      try {
        event = JSON.parse(message.data);
      } catch {
        return;
      }
      const [parsed] = parseRunEvents([event]);
      deliver(parsed);
      if (parsed?.type === 'verdict') finish(parsed);
    };
    source.onerror = () => {
      if (finished) return;
      source.close();
      source = null;
      poll();
    };
  });
}

export function createSelectionLoader(api) {
  let sequence = 0;
  let activeController = null;
  return {
    load(issueDate, turbine) {
      const requestSequence = ++sequence;
      activeController?.abort();
      const controller = new AbortController();
      activeController = controller;
      return api
        .loadForecastSelection(issueDate, turbine, { signal: controller.signal })
        .then((data) => {
          if (requestSequence !== sequence) return { stale: true, data: null };
          return { stale: false, data };
        })
        .catch((error) => {
          if (requestSequence !== sequence || error?.name === 'AbortError') return { stale: true, data: null };
          throw error;
        });
    },
    cancel() {
      sequence += 1;
      activeController?.abort();
    },
  };
}
