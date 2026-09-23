import { describe, expect, it } from 'vitest';
import { streamRunEvents } from './api';

class FakeSource {
  constructor(url) {
    this.url = url;
    FakeSource.last = this;
  }

  close() {
    this.closed = true;
  }
}

const event = (seq, type = 'tool_call') => ({ seq, type, title: `e${seq}`, meta: {} });

describe('agent run stream', () => {
  it('delivers SSE events in order and resolves on the verdict', async () => {
    const seen = [];
    const done = streamRunEvents('r_1', (item) => seen.push(item.seq), { EventSourceImpl: FakeSource, getRun: async () => ({}) });
    const source = FakeSource.last;
    expect(source.url).toBe('/api/runs/r_1/events');
    source.onmessage({ data: JSON.stringify(event(1)) });
    source.onmessage({ data: 'not json' });
    source.onmessage({ data: JSON.stringify(event(2, 'verdict')) });
    const result = await done;
    expect(seen).toEqual([1, 2]);
    expect(result.verdict.seq).toBe(2);
    expect(source.closed).toBe(true);
  });

  it('falls back to GET /api/runs/{id} without duplicating events when SSE drops', async () => {
    const seen = [];
    const done = streamRunEvents('r_2', (item) => seen.push(item.seq), {
      EventSourceImpl: FakeSource,
      getRun: async () => ({ done: true, events: [event(1), event(2), event(3, 'verdict')] }),
    });
    FakeSource.last.onmessage({ data: JSON.stringify(event(1)) });
    FakeSource.last.onerror();
    const result = await done;
    expect(seen).toEqual([1, 2, 3]);
    expect(result.verdict.seq).toBe(3);
  });
});
