// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createElement } from 'react';
import { act } from 'react-dom/test-utils';
import { createRoot } from 'react-dom/client';
import ChatPanel, { askAgent, formatTools, issueDateForTab } from './ChatPanel';

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let root;
let container;

function render(props) {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root.render(createElement(ChatPanel, props)));
}

function calendarWithSelected(day) {
  const calendar = document.createElement('div');
  calendar.id = 'calendar';
  calendar.innerHTML = `<button aria-selected="false"><span class="day-num">12</span></button>
    <button aria-selected="true"><span class="day-num">${day}</span></button>`;
  document.body.appendChild(calendar);
}

afterEach(() => {
  act(() => root?.unmount());
  root = null;
  document.body.innerHTML = '';
});

describe('ChatPanel', () => {
  it('stays hidden unless /health says ask: true', () => {
    render({ health: { ok: true, mode: 'agent' }, tab: 'february' });
    expect(container.querySelector('.chat-launch')).toBeNull();
    act(() => root.render(createElement(ChatPanel, { health: null, tab: 'february' })));
    expect(container.innerHTML).toBe('');
    act(() => root.render(createElement(ChatPanel, { health: { ok: true, ask: true }, tab: 'february' })));
    expect(container.querySelector('.chat-launch').textContent).toBe('Спросить агента');
  });

  it('sends the selected February issue date with the question', async () => {
    calendarWithSelected(13);
    const fetchImpl = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ answer: 'Пик 98 % — 15.02 в 16:00.', mode: 'agent', tools: [{ name: 'get_issue_summary', args: { issue_date: '2026-02-13' } }], issue_date: '2026-02-13' }),
    }));
    render({ health: { ok: true, ask: true }, tab: 'february', fetchImpl });
    act(() => container.querySelector('.chat-launch').click());
    await act(async () => container.querySelector('.chat-chip').click());

    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe('/api/ask');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual({ question: 'Когда пик в этом выпуске?', issue_date: '2026-02-13' });
    expect(container.querySelector('.chat-a').textContent).toContain('Пик 98 %');
    expect(container.querySelector('.chat-tools').textContent).toBe('данные: get_issue_summary(13.02)');
  });

  it('shows a Russian error when the agent times out', async () => {
    const fetchImpl = vi.fn(async () => ({ ok: false, status: 503, json: async () => { throw new Error('no body'); } }));
    render({ health: { ok: true, ask: true }, tab: 'quality', fetchImpl });
    act(() => container.querySelector('.chat-launch').click());
    await act(async () => container.querySelector('.chat-chip').click());
    expect(JSON.parse(fetchImpl.mock.calls[0][1].body).issue_date).toBeNull();
    expect(container.querySelector('.chat-err').textContent).toContain('30 секунд');
  });
});

describe('chat helpers', () => {
  it('maps tabs to issue_date', () => {
    calendarWithSelected(31);
    expect(issueDateForTab('february', document)).toBe('2026-01-31');
    expect(issueDateForTab('live', document)).toBe('live');
    expect(issueDateForTab('quality', document)).toBeNull();
  });

  it('formats tool calls and rejects malformed answers', async () => {
    expect(formatTools([{ name: 'get_hours', args: { issue_date: '2026-02-13', from_h: 1, to_h: 12, turbine: 'plant' } }, { name: 'get_quality', args: {} }]))
      .toBe('get_hours(13.02, 1, 12, plant), get_quality()');
    const fetchImpl = async () => ({ ok: true, status: 200, json: async () => ({ nope: true }) });
    await expect(askAgent({ question: 'x', issueDate: null, fetchImpl })).rejects.toThrow('неверном формате');
  });
});
