import { describe, expect, it } from 'vitest';
import { getStageSnapshots } from './AgentPanel';

describe('agent stage projection', () => {
  it('uses meta.stage and keeps a skipped recalc visible', () => {
    const stages = getStageSnapshots([
      { seq: 1, type: 'tool_call', meta: { stage: 'weather', status: 'ok' } },
      { seq: 2, type: 'tool_result', meta: { stage: 'weather', status: 'ok' } },
      { seq: 3, type: 'tool_call', meta: { stage: 'model', status: 'ok' } },
      { seq: 4, type: 'tool_result', meta: { stage: 'forecast', status: 'warn' } },
      { seq: 5, type: 'thought', meta: { stage: 'recalc', status: 'skip' } },
      { seq: 6, type: 'verdict', meta: { stage: null, status: 'ok' } },
    ]);
    expect(stages.find((stage) => stage.key === 'weather')).toMatchObject({ status: 'done' });
    expect(stages.find((stage) => stage.key === 'recalc')).toMatchObject({ status: 'skip' });
    expect(stages.find((stage) => stage.key === 'model')).toMatchObject({ status: 'done' });
    expect(stages.find((stage) => stage.key === 'forecast')).toMatchObject({ status: 'warn' });
  });
});
