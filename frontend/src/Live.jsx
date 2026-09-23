import { useCallback, useEffect, useState } from 'react';
import AgentPanel from './AgentPanel';
import ForecastChart from './ForecastChart';
import useAgentRun from './useAgentRun';
import { ApiError } from './api';
import { localFromUtc, localStamp, utcLabel } from './format';

const TURBINES = [
  { key: 'plant', label: 'ВЭС' },
  { key: '1', label: 'Турбина 1' },
  { key: '2', label: 'Турбина 2' },
];

const OUTCOME = {
  published: 'выпустил',
  kept: 'оставил как есть',
  refused: 'отказался',
  error: 'ошибка',
};

function errorText(error) {
  return error instanceof ApiError ? error.message : 'Нет связи с backend.';
}

function Journal({ entries, error }) {
  return (
    <section className="journal" id="live-journal">
      <h3>Журнал агента</h3>
      <p className="journal-sub">Каждые 30 минут агент сам проверяет новый прогон погоды и решает, пересчитывать ли выпуск.</p>
      {error && <p className="state-error">{error}</p>}
      {!error && entries.length === 0 && <p className="feed-empty">Записей пока нет — первая появится после проверки агентом.</p>}
      <ol>
        {entries.map((entry) => (
          <li key={`${entry.ts}-${entry.run_id}`} className={`jr jr-${entry.outcome}`} title={`${entry.title || ''}${entry.version ? ` · версия ${entry.version}` : ''}`}>
            <time>{localStamp(entry.ts)}</time>
            <span className={`who who-${entry.initiator}`}>{entry.initiator === 'agent' ? 'агент' : 'по кнопке'}</span>
            <span className="jr-outcome">{OUTCOME[entry.outcome] || entry.outcome}</span>
            <span className="jr-title">{entry.title}</span>
          </li>
        ))}
      </ol>
    </section>
  );
}

export default function Live({ api, active }) {
  const [status, setStatus] = useState(null);
  const [statusError, setStatusError] = useState(null);
  const [forecast, setForecast] = useState(null);
  const [forecastError, setForecastError] = useState(null);
  const [journal, setJournal] = useState([]);
  const [journalError, setJournalError] = useState(null);
  const [turbine, setTurbine] = useState('plant');
  const agent = useAgentRun(api);

  const refresh = useCallback(() => {
    api.getLiveStatus().then((value) => { setStatus(value); setStatusError(null); }).catch((error) => setStatusError(errorText(error)));
    api.getForecast('live', turbine).then((value) => { setForecast(value); setForecastError(null); }).catch((error) => { setForecast(null); setForecastError(errorText(error)); });
    api.getLiveJournal({ limit: 20 }).then((value) => { setJournal(value); setJournalError(null); }).catch((error) => setJournalError(errorText(error)));
  }, [api, turbine]);

  useEffect(() => {
    if (!active) return undefined;
    refresh();
    const timer = setInterval(refresh, 60 * 1000);
    return () => clearInterval(timer);
  }, [active, refresh]);

  const run = async (trigger, scenario = null) => {
    const caption = scenario ? 'Сбой погоды — проверяем, как агент справится' : trigger === 'issue' ? 'Выпуск от текущего момента' : 'Проверка обновлений погоды';
    await agent.start('live', trigger, { scenario, caption });
    refresh();
  };

  const cur = status?.current;

  return (
    <div className={`view view-live${active ? '' : ' hidden'}`}>
      <div className="main-col">
        <section className="live-status" id="live-status">
          {statusError ? <p className="state-error">{statusError}</p> : (
            <dl>
              <div><dt>Сейчас</dt><dd>{status ? localStamp(status.now_local) : '—'}</dd></div>
              <div><dt>Последний прогон</dt><dd>{status ? utcLabel(status.latest_run_utc) : '—'}</dd></div>
              <div><dt>Следующий будет доступен</dt><dd>{status ? `${localStamp(status.next_run_available_local)} мест.` : '—'}</dd></div>
              <div><dt>Текущий выпуск</dt><dd title={cur ? `версия ${cur.version}` : undefined}>{cur ? `от ${localStamp(cur.issued_at_local)}${cur.version > 1 ? ' · пересчитан' : ''}` : 'ещё нет'}</dd></div>
            </dl>
          )}
          {cur?.weather_run_utc && status?.latest_run_utc && Date.parse(cur.weather_run_utc) < Date.parse(status.latest_run_utc) && (
            <p className="note">Выпуск посчитан на прогоне {utcLabel(cur.weather_run_utc)}, а уже вышел {utcLabel(status.latest_run_utc)} — есть что проверить.</p>
          )}
          <div className="actions">
            <button type="button" className="btn primary" id="btn-live-issue" disabled={agent.running} onClick={() => run('issue')}>▶ Выпустить прогноз сейчас</button>
            <button type="button" className="btn" disabled={agent.running} onClick={() => run('new_weather_run')}>Проверить обновления погоды</button>
            <span className="spacer" />
            <button type="button" className="btn ghost" disabled={agent.running} onClick={() => run('issue', 'weather_outage')}>Имитировать сбой погоды</button>
          </div>
        </section>

        <section className="issue">
          <header className="issue-head">
            <div>
              <h1>Прогноз {turbine === 'plant' ? 'ВЭС' : `турбины ${turbine}`} на ближайшие 48 часов</h1>
              <p className="issue-sub">
                {forecast ? <span title={`версия ${forecast.version}`}>Сделан {localFromUtc(forecast.issue_time_utc)} по Астане · 48 часов{forecast.version > 1 ? ' · пересчитан после нового прогноза погоды' : ''}</span> : 'Open-Meteo Forecast, последний опубликованный прогон'}
              </p>
            </div>
            <div className="seg" role="radiogroup" aria-label="Объект" id="live-turbine-switch">
              {TURBINES.map((item) => (
                <button key={item.key} type="button" role="radio" aria-checked={turbine === item.key} className={turbine === item.key ? 'on' : ''} onClick={() => setTurbine(item.key)}>
                  {item.label}
                </button>
              ))}
            </div>
          </header>
          {forecast?.change_note && <p className="change-note">Пересчёт после нового прогноза погоды: {String(forecast.change_note).replace(/\s*против v\d+/, '')}.</p>}
          <div className="chart-card">
            {forecast ? (
              <>
                <ForecastChart rows={forecast.rows} flags={forecast.flags} />
                <div className="legend">
                  <span title="P50"><i className="lg-p50" />прогноз</span>
                  <span title="С вероятностью 80 % выработка будет в этом диапазоне (P10–P90)"><i className="lg-band" />вероятный диапазон (80 %)</span>
                  <span><i className="lg-wind" />ветер</span>
                </div>
              </>
            ) : (
              <div className="chart-empty">{forecastError || 'Загрузка…'}</div>
            )}
          </div>
        </section>

        <Journal entries={journal} error={journalError} />
      </div>

      <AgentPanel
        events={agent.events}
        running={agent.running}
        caption={agent.caption}
        error={agent.error}
        summary={!agent.running && !agent.events.length ? forecast?.summary : null}
        idleHint="Нажмите «Выпустить прогноз сейчас» — агент возьмёт свежую погоду и покажет каждый шаг."
      />
    </div>
  );
}
