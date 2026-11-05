import { expect, it } from 'vitest';
import { threadHistoryQuery, readThreadHistory } from '../../src/frontend/src/shared/lib/threadHistory';

it('按正式逻辑范围读取同编号线程，拒绝逐轮来源混合及缺失证明', () => {
  const turn = { id: 'turn-one', project_id: 'same-project', thread_id: 'same-thread', intent: 'ask', user_text: '合成问题', receipt: { ask: { answer: '合成回答' } } };
  for (const kind of ['personal', 'shared']) {
    expect(new URLSearchParams(threadHistoryQuery('same-project', kind)).get('scope_kind')).toBe(kind);
    const value = { id: 'same-thread', scope_kind: kind, turns: [{ ...turn, scope_kind: kind }] };
    expect(readThreadHistory(value, 'same-thread', 'same-project', kind)).toBe(value);
    expect(() => readThreadHistory({ ...value, turns: [{ ...turn, scope_kind: kind === 'shared' ? 'personal' : 'shared' }] }, 'same-thread', 'same-project', kind)).toThrow();
  }
  expect(() => readThreadHistory({ id: 'same-thread', turns: [turn] }, 'same-thread', 'same-project')).toThrow();
});
