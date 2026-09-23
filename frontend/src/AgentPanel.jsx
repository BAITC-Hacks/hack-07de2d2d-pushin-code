import { useEffect, useRef } from 'react';

export const AGENT_STAGES = [
  { key: 'weather', label: 'Погода' },
  { key: 'prep', label: 'Подготовка' },
  { key: 'model', label: 'Модель' },
  { key: 'forecast', label: 'Прогноз' },
  { key: 'analysis', label: 'Анализ' },
  { key: 'recalc', label: 'Пересчёт' },
];

const TYPE_LABELS = {
  thought: 'рассуждение',
  tool_call: 'вызов',
  tool_result: 'результат',
  action: 'действие',
  verdict: 'итог',
  error: 'ошибка',
};

const STATUS_TEXT = {
  pending: 'ждёт',
  active: 'идёт',
  done: 'готово',
  skip: 'не нужен',
  warn: 'есть риски',
  error: 'ошибка',
};

function stageStatus(event, hasLaterStage, hasLaterVerdict) {
  if (!event) return 'pending';
  if (event.meta?.status === 'skip') return 'skip';
  if (event.meta?.status === 'warn') return 'warn';
  if (event.meta?.status === 'error' || event.type === 'error') return 'error';
  if ((event.type === 'tool_call' || event.type === 'thought') && !hasLaterStage && !hasLaterVerdict) return 'active';
  return 'done';
}

export function getStageSnapshots(events = []) {
  return AGENT_STAGES.map((stage) => {
    const eventIndex = events.reduce((last, candidate, index) => (candidate?.meta?.stage === stage.key ? index : last), -1);
    const event = eventIndex >= 0 ? events[eventIndex] : null;
    const rest = eventIndex >= 0 ? events.slice(eventIndex + 1) : [];
    const hasLaterStage = rest.some((candidate) => candidate?.meta?.stage);
    const hasLaterVerdict = rest.some((candidate) => candidate?.type === 'verdict');
    return { ...stage, status: stageStatus(event, hasLaterStage, hasLaterVerdict), event };
  });
}

function eventTime(ts) {
  const match = String(ts || '').match(/T(\d{2}:\d{2}:\d{2})/);
  return match ? match[1] : '';
}

export default function AgentPanel({ events = [], running = false, caption = null, error = null, summary = null, idleHint }) {
  const feedRef = useRef(null);
  const stages = getStageSnapshots(events);
  const verdict = [...events].reverse().find((event) => event.type === 'verdict');

  useEffect(() => {
    const feed = feedRef.current;
    if (feed && running) feed.scrollTop = feed.scrollHeight;
  }, [events.length, running]);

  return (
    <aside className="agent" id="agent-panel" aria-live="polite">
      <header className="agent-head">
        <h2>Агент</h2>
        <span className={`agent-state${running ? ' on' : ''}`}>{running ? 'работает' : events.length ? 'закончил' : 'ждёт'}</span>
      </header>

      <ol className="stages" id="agent-stages">
        {stages.map((stage) => (
          <li key={stage.key} className={`stage s-${stage.status}`} title={stage.event?.title || ''}>
            <span className="stage-dot" />
            <span className="stage-name">{stage.label}</span>
            <span className="stage-status">{STATUS_TEXT[stage.status]}</span>
          </li>
        ))}
      </ol>

      {caption && <p className="agent-caption">{caption}</p>}
      {error && <p className="agent-error" role="alert">{error}</p>}

      <div className="feed" ref={feedRef}>
        {events.length === 0 && !running && <p className="feed-empty">{idleHint || 'Здесь появятся шаги агента.'}</p>}
        <ol>
          {events.map((event) => (
            <li key={`${event.meta?.issue_date || ''}-${event.seq}`} className={`ev ev-${event.type} st-${event.meta?.status || 'ok'}`}>
              <div className="ev-meta">
                <span className="ev-type">{TYPE_LABELS[event.type] || event.type}</span>
                {event.meta?.tool && <code>{event.meta.tool}</code>}
                {event.meta?.version ? <span className="ev-ver">v{event.meta.version}</span> : null}
                {event.meta?.source && event.meta.source !== 'api' && <span className="ev-src">{event.meta.source === 'cache' ? 'кэш' : event.meta.source}</span>}
                <time>{eventTime(event.ts)}</time>
              </div>
              <p className="ev-title">{event.title}</p>
              {event.body && <p className="ev-body">{event.body}</p>}
            </li>
          ))}
        </ol>
        {running && <p className="feed-typing">агент думает…</p>}
      </div>

      {(verdict || summary) && (
        <section className="dispatch" id="dispatch-summary">
          <h3>Сводка для диспетчера</h3>
          <p>{summary || verdict?.body || verdict?.title}</p>
        </section>
      )}
    </aside>
  );
}
