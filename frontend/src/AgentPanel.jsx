import { useEffect, useRef, useState } from 'react';

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

const DECISION_TEXT = {
  recalc: 'пересчитать',
  keep: 'оставить как есть',
  refused: 'отказаться',
};

const DECISION_WHY = {
  recalc: 'свежий прогноз погоды',
  keep: 'прогноз почти не изменился',
  refused: 'нет подходящих данных',
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

function eventText(event) {
  return `${event?.title || ''} ${event?.body || ''}`;
}

// «Решение агента: пересчитать — свежий прогноз погоды, ветер +0,7 м/с» from the decision event.
export function decisionLine(events = []) {
  let index = -1;
  events.forEach((event, i) => { if (DECISION_TEXT[event?.meta?.decision]) index = i; });
  if (index < 0) return null;
  const event = events[index];
  const decision = event.meta.decision;
  const by = event.meta.by === 'rule' ? 'по правилу' : event.meta.by ? 'LLM' : null;
  let wind = '';
  for (let i = index; i >= 0 && !wind; i -= 1) {
    const match = eventText(events[i]).match(/([+−]\d+(?:,\d+)?)\s*м\/с/);
    if (match) wind = `, ветер ${match[1]} м/с`;
  }
  if (!wind) {
    const match = eventText(event).match(/сдвиг[^=—]*[=—]\s*(\d+(?:[.,]\d+)?)\s*м\/с/);
    if (match) wind = `, сдвиг ветра ${match[1].replace('.', ',')} м/с`;
  }
  return { decision, by, text: `Решение агента: ${DECISION_TEXT[decision]} — ${DECISION_WHY[decision]}${wind}` };
}

function eventTime(ts) {
  const match = String(ts || '').match(/T(\d{2}:\d{2})/);
  return match ? match[1] : '';
}

// First two sentences; the rest opens on «ещё».
export function splitSummary(text) {
  const parts = String(text || '').trim().split(/(?<=[.!?])\s+/).filter(Boolean);
  if (parts.length <= 1) return { lead: parts[0] || '', rest: '' };
  const take = parts[0].length + parts[1].length < 200 ? 2 : 1;
  return { lead: parts.slice(0, take).join(' '), rest: parts.slice(take).join(' ') };
}

function Summary({ text }) {
  const [open, setOpen] = useState(false);
  useEffect(() => setOpen(false), [text]);
  const { lead, rest } = splitSummary(text);
  return (
    <p>
      {lead}{open && rest ? ` ${rest}` : ''}
      {rest && !open && <> <button type="button" className="link more" onClick={() => setOpen(true)}>ещё</button></>}
    </p>
  );
}

export default function AgentPanel({ events = [], running = false, caption = null, error = null, summary = null, idleHint }) {
  const feedRef = useRef(null);
  const stages = getStageSnapshots(events);
  const verdict = [...events].reverse().find((event) => event.type === 'verdict');
  const decision = decisionLine(events);

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

      {decision && (
        <p className={`decision decision-${decision.decision}`} id="agent-decision">
          <span>{decision.text}</span>
          {decision.by && <span className="decision-by">{decision.by}</span>}
        </p>
      )}

      {caption && <p className="agent-caption">{caption}</p>}
      {error && <p className="agent-error" role="alert">{error}</p>}

      <div className="feed" ref={feedRef}>
        {events.length === 0 && !running && <p className="feed-empty">{idleHint || 'Здесь появятся шаги агента.'}</p>}
        <ol>
          {events.map((event) => {
            const hint = [TYPE_LABELS[event.type] || event.type, event.meta?.tool, event.meta?.version ? `версия ${event.meta.version}` : null].filter(Boolean).join(' · ');
            return (
              <li key={`${event.meta?.issue_date || ''}-${event.seq}`} className={`ev ev-${event.type} st-${event.meta?.status || 'ok'}`}>
                <details>
                  <summary title={hint}>
                    <span className="ev-title">{event.title}</span>
                    {event.meta?.source && event.meta.source !== 'api' && <span className="ev-src">{event.meta.source === 'cache' ? 'кэш' : event.meta.source}</span>}
                    <time>{eventTime(event.ts)}</time>
                  </summary>
                  {event.body && <p className="ev-body">{event.body}</p>}
                </details>
              </li>
            );
          })}
        </ol>
        {running && <p className="feed-typing">агент думает…</p>}
      </div>

      {(verdict || summary) && (
        <section className="dispatch" id="dispatch-summary">
          <h3>Сводка для диспетчера</h3>
          <Summary text={summary || verdict?.body || verdict?.title} />
        </section>
      )}
    </aside>
  );
}
