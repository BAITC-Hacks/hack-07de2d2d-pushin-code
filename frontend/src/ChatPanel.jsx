import { useEffect, useRef, useState } from 'react';
import './chat.css';

export const PRESET_QUESTIONS = [
  'Когда пик в этом выпуске?',
  'Какие риски и когда?',
  'Почему агент пересчитал прогноз?',
  'Насколько точна модель?',
];

const MAX_QUESTION = 500;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

// The selected February day lives inside February.jsx; read it from the calendar
// instead of lifting state. Issues run 31.01 → 28.02, so day 31 is January.
export function selectedFebruaryDay(doc = typeof document === 'undefined' ? null : document) {
  const button = doc?.querySelector('#calendar [aria-selected="true"]');
  const day = Number(button?.querySelector('.day-num')?.textContent);
  if (!Number.isInteger(day) || day < 1 || day > 31) return null;
  return day === 31 ? '2026-01-31' : `2026-02-${String(day).padStart(2, '0')}`;
}

export function issueDateForTab(tab, doc) {
  if (tab === 'february') return selectedFebruaryDay(doc);
  if (tab === 'live') return 'live';
  return null;
}

function formatArg(value) {
  if (typeof value === 'string' && ISO_DATE.test(value)) return `${value.slice(8, 10)}.${value.slice(5, 7)}`;
  return String(value);
}

export function formatTools(tools) {
  if (!Array.isArray(tools) || tools.length === 0) return '';
  return tools
    .filter((tool) => tool && tool.name)
    .map((tool) => {
      const args = tool.args && typeof tool.args === 'object' ? Object.values(tool.args).filter((value) => value !== null && value !== undefined) : [];
      return `${tool.name}(${args.map(formatArg).join(', ')})`;
    })
    .join(', ');
}

export async function askAgent({ question, issueDate, fetchImpl = globalThis.fetch, signal }) {
  let response;
  try {
    response = await fetchImpl('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ question, issue_date: issueDate ?? null }),
      signal,
    });
  } catch (error) {
    if (error?.name === 'AbortError') throw error;
    throw new Error('Нет связи с сервером. Проверьте, что backend запущен.');
  }
  let body = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (!response.ok) {
    if (response.status === 503) throw new Error(body?.error || 'Агент не успел ответить за 30 секунд. Попробуйте ещё раз.');
    if (response.status === 400) throw new Error(body?.error || 'Вопрос должен быть от 1 до 500 символов.');
    throw new Error(body?.error || `Агент не ответил (ошибка ${response.status}).`);
  }
  if (!body || typeof body.answer !== 'string') throw new Error('Агент вернул ответ в неверном формате.');
  return body;
}

function contextLabel(issueDate) {
  if (issueDate === 'live') return 'вкладка Live';
  if (issueDate) return `выпуск ${formatArg(issueDate)}`;
  return 'без выбранного выпуска';
}

export default function ChatPanel({ health, tab, fetchImpl }) {
  const [open, setOpen] = useState(false);
  const [question, setQuestion] = useState('');
  const [entries, setEntries] = useState([]);
  const [busy, setBusy] = useState(false);
  const inputRef = useRef(null);
  const feedRef = useRef(null);
  const controllerRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    inputRef.current?.focus();
    const onKey = (event) => event.key === 'Escape' && setOpen(false);
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open]);

  useEffect(() => {
    const feed = feedRef.current;
    if (feed) feed.scrollTop = feed.scrollHeight;
  }, [entries]);

  useEffect(() => () => controllerRef.current?.abort(), []);

  if (!health?.ask) return null;

  const issueDate = issueDateForTab(tab);

  const send = async (text) => {
    const trimmed = text.trim();
    if (!trimmed || busy) return;
    const id = Date.now();
    const date = issueDateForTab(tab);
    setEntries((list) => [...list, { id, question: trimmed, context: contextLabel(date), status: 'pending' }]);
    setQuestion('');
    setBusy(true);
    const controller = new AbortController();
    controllerRef.current = controller;
    try {
      const result = await askAgent({ question: trimmed, issueDate: date, fetchImpl, signal: controller.signal });
      setEntries((list) => list.map((entry) => (entry.id === id ? { ...entry, status: 'done', answer: result.answer, tools: formatTools(result.tools), mode: result.mode } : entry)));
    } catch (error) {
      if (error?.name === 'AbortError') return;
      setEntries((list) => list.map((entry) => (entry.id === id ? { ...entry, status: 'error', error: error.message } : entry)));
    } finally {
      if (controllerRef.current === controller) controllerRef.current = null;
      setBusy(false);
    }
  };

  const onSubmit = (event) => {
    event.preventDefault();
    send(question);
  };

  return (
    <>
      {!open && (
        <button type="button" className="chat-launch" onClick={() => setOpen(true)} aria-expanded="false" aria-controls="chat-panel">
          Спросить агента
        </button>
      )}
      <aside id="chat-panel" className={`chat-panel${open ? ' on' : ''}`} aria-hidden={!open} aria-label="Спросить агента">
        <header className="chat-head">
          <div>
            <h2>Спросить агента</h2>
            <p>Отвечает по данным системы · {contextLabel(issueDate)}</p>
          </div>
          <button type="button" className="icon-btn" onClick={() => setOpen(false)} aria-label="Закрыть">×</button>
        </header>

        <div className="chat-feed" ref={feedRef} aria-live="polite">
          {entries.length === 0 && <p className="chat-empty">Задайте вопрос о прогнозе, рисках или точности модели — или выберите готовый ниже.</p>}
          {entries.map((entry) => (
            <div className="chat-entry" key={entry.id}>
              <p className="chat-q">{entry.question}<span>{entry.context}</span></p>
              {entry.status === 'pending' && <p className="chat-a chat-wait">думаю…</p>}
              {entry.status === 'error' && <p className="chat-a chat-err" role="alert">{entry.error}</p>}
              {entry.status === 'done' && (
                <div className="chat-a">
                  <p>{entry.answer}</p>
                  {entry.tools && <p className="chat-tools">данные: {entry.tools}</p>}
                </div>
              )}
            </div>
          ))}
        </div>

        <div className="chat-chips">
          {PRESET_QUESTIONS.map((preset) => (
            <button key={preset} type="button" className="chat-chip" disabled={busy} onClick={() => send(preset)}>{preset}</button>
          ))}
        </div>

        <form className="chat-form" onSubmit={onSubmit}>
          <label className="chat-sr" htmlFor="chat-input">Ваш вопрос</label>
          <input
            id="chat-input"
            ref={inputRef}
            type="text"
            value={question}
            maxLength={MAX_QUESTION}
            placeholder="Например: когда ветер выше номинала?"
            onChange={(event) => setQuestion(event.target.value)}
            disabled={busy}
            autoComplete="off"
          />
          <button type="submit" className="btn primary" disabled={busy || !question.trim()}>Спросить</button>
        </form>
      </aside>
    </>
  );
}
