import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import AgentPanel from './AgentPanel';
import ForecastChart from './ForecastChart';
import useAgentRun from './useAgentRun';
import { ApiError, createSelectionLoader, weatherRunIsBeforeIssue } from './api';
import { FLAG_KINDS, dayLabel, rangeLabel, localFromUtc, localStamp, nextDay, pct, shortDate, utcLabel, weekday } from './format';

const TURBINES = [
  { key: 'plant', label: 'ВЭС' },
  { key: '1', label: 'Турбина 1' },
  { key: '2', label: 'Турбина 2' },
];

const DEMO_DAY = '2026-02-13';

function errorText(error) {
  return error instanceof ApiError ? error.message : 'Не удалось получить данные. Проверьте, что backend запущен.';
}

function flagTotal(flags) {
  return Object.values(flags || {}).reduce((sum, value) => sum + (Number(value) || 0), 0);
}

function Calendar({ issues, selected, onSelect, playing }) {
  return (
    <div className="calendar" id="calendar" role="listbox" aria-label="Выпуски 31 января — 28 февраля">
      {issues.map((issue) => {
        const flags = flagTotal(issue.flags);
        const recalculated = (issue.versions || []).length > 1;
        const height = Math.max(4, Math.round((issue.mean_p50 || 0) * 100));
        return (
          <button
            key={issue.issue_date}
            type="button"
            role="option"
            aria-selected={issue.issue_date === selected}
            className={`day${issue.issue_date === selected ? ' sel' : ''}${issue.status !== 'published' ? ' missing' : ''}${playing && issue.issue_date === selected ? ' playing' : ''}`}
            onClick={() => onSelect(issue.issue_date)}
            title={`Выпуск ${shortDate(issue.issue_date)} · среднее ${pct(issue.mean_p50)} · пик ${pct(issue.peak_p50)}${flags ? ` · рисков: ${flags}` : ''}${recalculated ? ' · был пересчёт' : ''}`}
          >
            <span className="day-wd">{weekday(issue.issue_date)}</span>
            <span className="day-bar"><span style={{ height: `${height}%` }} /></span>
            <span className="day-num">{Number(issue.issue_date.slice(8, 10))}</span>
            <span className="day-marks">
              {flags > 0 && <i className="mark-flag" aria-label="есть риски" />}
              {recalculated && <b>v{issue.version}</b>}
            </span>
          </button>
        );
      })}
    </div>
  );
}

export function provenanceState(forecast) {
  const runs = forecast?.weather_runs;
  if (!Array.isArray(runs) || runs.length === 0) return 'unknown';
  const issueTime = Date.parse(forecast.issue_time_utc || `${forecast.issue_date}T19:00:00Z`);
  return runs.every((run) => weatherRunIsBeforeIssue(run, issueTime)) ? 'ok' : 'warn';
}

function Provenance({ forecast }) {
  const [open, setOpen] = useState(false);
  const state = provenanceState(forecast);
  useEffect(() => setOpen(false), [forecast?.issue_date]);
  const issueUtc = forecast?.issue_time_utc || `${forecast?.issue_date}T19:00:00Z`;
  return (
    <div className={`proof proof-${state}`} id="no-future">
      <button type="button" className="proof-toggle" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
        <span className="proof-mark" aria-hidden="true">{state === 'ok' ? '✓' : '!'}</span>
        {state === 'ok' ? 'Без будущего' : state === 'warn' ? 'Проверьте прогоны' : 'Нет данных о прогонах'}
        <span className="chev" aria-hidden="true">{open ? '−' : '+'}</span>
      </button>
      {open && (
        <div className="proof-body">
          <p>
            Момент выпуска — <b>{localFromUtc(issueUtc)}</b> по Астане ({utcLabel(issueUtc)}).
            Прогон погоды публикуется примерно через 8 часов после старта, поэтому брать можно только прогоны,
            стартовавшие не позже чем за 8 часов до выпуска.
          </p>
          <table>
            <thead><tr><th>Часы</th><th>Модель</th><th>Старт прогона</th><th>Доступен с</th><th /></tr></thead>
            <tbody>
              {forecast.weather_runs.map((run) => {
                const ok = weatherRunIsBeforeIssue(run, Date.parse(issueUtc));
                const available = new Date(Date.parse(run.init_utc) + 8 * 3600 * 1000).toISOString();
                return (
                  <tr key={`${run.hours}-${run.init_utc}`}>
                    <td>{run.hours}</td>
                    <td><code>{run.model}</code></td>
                    <td>{utcLabel(run.init_utc)}</td>
                    <td>{localFromUtc(available)} мест.</td>
                    <td className={ok ? 'ok' : 'bad'}>{ok ? 'до выпуска' : 'после выпуска'}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function FlagList({ flags }) {
  if (!flags.length) return <p className="flags-none">Рисков в окне не найдено.</p>;
  return (
    <ul className="flags">
      {flags.map((flag, index) => (
        <li key={`${flag.kind}-${index}`} className={`flag-item flag-${flag.kind}`}>
          <i />
          <span className="flag-kind">{FLAG_KINDS[flag.kind]?.label || flag.kind}</span>
          <span className="flag-text">{flag.text}</span>
        </li>
      ))}
    </ul>
  );
}

export default function February({ api, active }) {
  const [issues, setIssues] = useState([]);
  const [issuesError, setIssuesError] = useState(null);
  const [selected, setSelected] = useState(DEMO_DAY);
  const [turbine, setTurbine] = useState('plant');
  const [selection, setSelection] = useState({ loading: true, data: null, error: null });
  const [reloadKey, setReloadKey] = useState(0);
  const [playing, setPlaying] = useState(false);
  const playRef = useRef(false);
  const agent = useAgentRun(api);
  const loader = useMemo(() => createSelectionLoader(api), [api]);

  const loadIssues = useCallback(() => {
    api.getIssues()
      .then((list) => {
        setIssues(list);
        setIssuesError(null);
        setSelected((current) => (list.some((issue) => issue.issue_date === current) ? current : list[list.length - 1]?.issue_date || current));
      })
      .catch((error) => setIssuesError(errorText(error)));
  }, [api]);

  useEffect(loadIssues, [loadIssues]);

  useEffect(() => {
    let cancelled = false;
    setSelection((current) => ({ ...current, loading: true, error: null }));
    loader.load(selected, turbine)
      .then(({ stale, data }) => {
        if (!stale && !cancelled) setSelection({ loading: false, data, error: null });
      })
      .catch((error) => {
        if (!cancelled) setSelection({ loading: false, data: null, error: errorText(error) });
      });
    return () => { cancelled = true; };
  }, [loader, selected, turbine, reloadKey]);

  // Show the saved trace of the selected day unless something is running.
  useEffect(() => {
    if (playRef.current || agent.running) return undefined;
    const controller = new AbortController();
    api.getTrace(selected, { signal: controller.signal })
      .then((trace) => agent.show(trace, trace.recorded_at ? `Запись прогона от ${localStamp(trace.recorded_at)}` : 'Сохранённая лента'))
      .catch((error) => { if (error?.name !== 'AbortError') agent.show(null, null); });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, selected]);

  const runAgent = async (trigger) => {
    const caption = trigger === 'issue' ? `Перевыпуск ${shortDate(selected)} — в реальном времени` : `Новый прогон погоды для ${shortDate(selected)}`;
    await agent.start(selected, trigger, { caption });
    loadIssues();
    setReloadKey((key) => key + 1);
  };

  const playFebruary = async () => {
    if (playRef.current) {
      playRef.current = false;
      agent.stop();
      setPlaying(false);
      return;
    }
    playRef.current = true;
    setPlaying(true);
    for (const issue of issues) {
      if (!playRef.current) break;
      setSelected(issue.issue_date);
      try {
        const trace = await api.getTrace(issue.issue_date);
        if (!playRef.current) break;
        await agent.replay(trace, { delayMs: 22, caption: `Воспроизведение · выпуск ${shortDate(issue.issue_date)}${trace.recorded_at ? ` · запись от ${localStamp(trace.recorded_at)}` : ''}` });
      } catch {
        // A missing trace should not stop the whole month.
      }
    }
    playRef.current = false;
    setPlaying(false);
  };

  const current = selection.data?.current;
  const previous = selection.data?.previous;
  const issue = issues.find((item) => item.issue_date === selected);
  const moment = nextDay(selected);

  return (
    <div className={`view view-feb${active ? '' : ' hidden'}`}>
      <div className="main-col">
        <section className="time-machine" id="time-machine">
          <p>
            Для системы сейчас <b>{moment ? `${dayLabel(moment)} 2026, 00:00` : '—'}</b>.
            Всё, что правее на графике, она ещё не видела.
          </p>
          <div className="tm-actions">
            <button type="button" className="btn" id="play-february" onClick={playFebruary} disabled={!issues.length}>
              {playing ? 'Остановить' : '▶ Воспроизвести февраль'}
            </button>
            <a className="btn ghost" href={api.summaryCsvUrl()} download>Все 29 выпусков, CSV</a>
          </div>
        </section>

        {issuesError ? (
          <p className="state-error" role="alert">{issuesError} <button type="button" className="link" onClick={loadIssues}>Повторить</button></p>
        ) : (
          <Calendar issues={issues} selected={selected} onSelect={(day) => !playing && setSelected(day)} playing={playing} />
        )}
        <p className="calendar-legend">
          Высота столбика — средняя прогнозная выработка ВЭС. <i className="mark-flag" /> есть риски · <b>v2</b> агент пересчитал по свежему прогону.
        </p>

        <section className="issue">
          <header className="issue-head">
            <div>
              <h1>Выпуск на {moment ? rangeLabel(moment, nextDay(moment)) : '—'}</h1>
              <p className="issue-sub">
                Сделан {moment ? `${shortDate(moment)} в 00:00` : '—'} по Астане · 48 часов
                {current && <> · версия {current.version}{current.versions?.length > 1 ? ` из ${current.versions.length}` : ''}</>}
                {current?.model_version && <> · <code>{current.model_version}</code></>}
              </p>
            </div>
            <div className="seg" role="radiogroup" aria-label="Объект" id="turbine-switch">
              {TURBINES.map((item) => (
                <button key={item.key} type="button" role="radio" aria-checked={turbine === item.key} className={turbine === item.key ? 'on' : ''} onClick={() => setTurbine(item.key)}>
                  {item.label}
                </button>
              ))}
            </div>
          </header>

          {current?.change_note && (
            <p className="change-note"><b>v{current.version} против v{current.version - 1}:</b> {current.change_note}</p>
          )}

          <div className="chart-card" id="forecast-chart">
            {selection.error ? (
              <p className="state-error" role="alert">{selection.error}</p>
            ) : current ? (
              <>
                <div className={selection.loading ? 'loading-dim' : ''}>
                  <ForecastChart rows={current.rows} previousRows={previous?.rows || null} flags={current.flags} />
                </div>
                <div className="legend">
                  <span><i className="lg-p50" />P50, медиана</span>
                  <span><i className="lg-band" />P10–P90, 8 из 10 исходов</span>
                  {previous && <span><i className="lg-prev" />v{previous.version}, до пересчёта</span>}
                  <span><i className="lg-wind" />ветер</span>
                </div>
              </>
            ) : (
              <div className="skeleton" aria-busy="true" />
            )}
          </div>

          {current && (
            <div className="issue-foot">
              <div className="foot-block">
                <h3>Риски в окне</h3>
                <FlagList flags={current.flags} />
              </div>
              <div className="foot-block">
                <h3>Проверка</h3>
                <Provenance forecast={current} />
                {issue?.source === 'cache' && <p className="note">Погода взята из кэша: Open-Meteo был недоступен.</p>}
              </div>
            </div>
          )}

          <div className="actions" id="issue-actions">
            <a className="btn ghost" href={api.csvUrl(selected)} download>⬇ CSV выпуска</a>
            <span className="spacer" />
            <button type="button" className="btn" id="btn-reissue" disabled={agent.running || playing} onClick={() => runAgent('issue')}>
              ↻ Перевыпустить агентом
            </button>
            <button type="button" className="btn" id="btn-new-run" disabled={agent.running || playing} onClick={() => runAgent('new_weather_run')}>
              Новый прогон погоды
            </button>
          </div>
        </section>
      </div>

      <AgentPanel
        events={agent.events}
        running={agent.running}
        caption={agent.caption}
        error={agent.error}
        summary={!agent.running ? current?.summary : null}
        idleHint="Выберите день или нажмите «Перевыпустить агентом» — шаги агента появятся здесь."
      />
    </div>
  );
}
