import { describe, expect, it } from 'vitest';
import {
  createApi,
  createSelectionLoader,
  fixtureNamesForPath,
  parseForecast,
  parseIssues,
  replayRunEvents,
  weatherRunIsBeforeIssue,
} from './api';

const issuePayload = [
  { issue_date: '2026-02-13', status: 'published', version: 2, versions: [1, 2], mean_p50: 0.41, peak_p50: 0.88, flags: { ramp: 1 }, source: 'api' },
  { issue_date: '2026-01-31', status: 'published', version: 1, versions: [1], mean_p50: 0.2, peak_p50: 0.4, flags: {}, source: 'cache' },
];

const forecastPayload = (version = 2, turbine = 'plant') => ({
  issue_date: '2026-02-13', issue_time_utc: '2026-02-13T19:00:00Z', version, versions: [1, 2],
  weather_runs: [{ hours: '1-24', model: 'ecmwf_ifs025', init_utc: '2026-02-13T00:00:00Z', before_issue: true }],
  rows: [
    { h: 1, turbine, target_time_local: '2026-02-14T00:00:00+05:00', p10: 0.1, p50: 0.2, p90: 0.3, wind_fc_ms: null, temp_fc_c: -1, actual: null },
    { h: 2, turbine, target_time_local: '2026-02-14T01:00:00+05:00', p10: 0.2, p50: 0.3, p90: 0.4, wind_fc_ms: 12, temp_fc_c: 0, actual: 0.31 },
  ],
});

function response(body, ok = true, status = 200) {
  return { ok, status, json: async () => body };
}

describe('API parsing and selection', () => {
  it('maps latest and v2 to the current fixture and v1 to the conditional suffix', () => {
    expect(fixtureNamesForPath('/api/forecasts/2026-02-13?version=latest&turbine=plant')).toBe('forecasts_2026-02-13.json');
    expect(fixtureNamesForPath('/api/forecasts/2026-02-13?version=2&turbine=plant')).toBe('forecasts_2026-02-13.json');
    expect(fixtureNamesForPath('/api/forecasts/2026-02-13?version=1&turbine=plant')).toBe('forecasts_2026-02-13_v1.json');
  });

  it('sorts issues and preserves the contract fields', () => {
    const issues = parseIssues(issuePayload);
    expect(issues.map((issue) => issue.issue_date)).toEqual(['2026-01-31', '2026-02-13']);
    expect(issues[1].mean_p50).toBe(0.41);
    expect(issues[1].flags.ramp).toBe(1);
  });

  it('selects the requested turbine and keeps missing actual/wind values missing', () => {
    const forecast = parseForecast({ ...forecastPayload(), rows: [...forecastPayload().rows, { ...forecastPayload().rows[0], turbine: '1', p50: 0.9 }] }, 'plant');
    expect(forecast.rows).toHaveLength(2);
    expect(forecast.rows[0].wind_fc_ms).toBeNull();
    expect(forecast.rows[0].actual).toBeNull();
  });

  it('filters all three string turbine rows on the client', () => {
    const rows = ['1', '2', 'plant'].map((turbine) => ({ ...forecastPayload().rows[0], turbine }));
    expect(parseForecast({ ...forecastPayload(), rows }, '1').rows[0].turbine).toBe('1');
    expect(parseForecast({ ...forecastPayload(), rows }, '2').rows[0].turbine).toBe('2');
    expect(parseForecast({ ...forecastPayload(), rows }, 'plant').rows[0].turbine).toBe('plant');
  });

  it('uses relative API endpoints and keeps a current forecast when the previous version is unavailable', async () => {
    const calls = [];
    const fetchImpl = async (path) => {
      calls.push(path);
      if (path.includes('issues')) return response(issuePayload);
      if (path.includes('version=1')) return response({ error: 'нет версии' }, false, 404);
      return response(forecastPayload());
    };
    const api = createApi({ fetchImpl });
    expect((await api.getIssues()).length).toBe(2);
    const result = await api.loadForecastSelection('2026-02-13', 'plant');
    expect(calls[0]).toBe('/api/issues?from=2026-01-31&to=2026-02-28');
    expect(calls.some((path) => path === '/api/forecasts/2026-02-13?version=latest&turbine=plant')).toBe(true);
    expect(result.current.rows).toHaveLength(2);
    expect(result.previous).toBeNull();
    expect(result.previousError.status).toBe(404);
  });

  it('marks an older selection response stale when a newer selection wins the race', async () => {
    const pending = new Map();
    const api = { loadForecastSelection: (date) => new Promise((resolve) => pending.set(date, resolve)) };
    const loader = createSelectionLoader(api);
    const first = loader.load('2026-02-12', 'plant');
    const second = loader.load('2026-02-13', 'plant');
    pending.get('2026-02-12')({ current: { issue_date: '2026-02-12' }, previous: null });
    pending.get('2026-02-13')({ current: { issue_date: '2026-02-13' }, previous: null });
    await expect(first).resolves.toEqual({ stale: true, data: null });
    await expect(second).resolves.toEqual({ stale: false, data: { current: { issue_date: '2026-02-13' }, previous: null } });
  });

  it('sorts run events by seq and closes replay on the verdict', async () => {
    const delivered = [];
    const result = await replayRunEvents([
      { seq: 3, type: 'verdict', meta: { stage: null } },
      { seq: 1, type: 'thought', meta: { stage: null } },
      { seq: 2, type: 'action', meta: { stage: null } },
      { seq: 4, type: 'thought', meta: { stage: null } },
    ], (event) => delivered.push(event.seq), { delayMs: 0 });
    expect(delivered).toEqual([1, 2, 3]);
    expect(result.closed).toBe(true);
    expect(result.verdict.seq).toBe(3);
  });

  it('requires the eight-hour publication delay for a no-future weather run', () => {
    const issueTime = '2026-02-13T19:00:00Z';
    expect(weatherRunIsBeforeIssue({ init_utc: '2026-02-13T10:00:00Z', before_issue: true }, issueTime)).toBe(true);
    expect(weatherRunIsBeforeIssue({ init_utc: '2026-02-13T11:30:00Z', before_issue: true }, issueTime)).toBe(false);
    expect(weatherRunIsBeforeIssue({ init_utc: '2026-02-13T10:00:00Z', before_issue: false }, issueTime)).toBe(false);
  });
});
