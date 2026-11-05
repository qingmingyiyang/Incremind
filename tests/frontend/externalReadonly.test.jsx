import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { expect, it } from 'vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import { SharedHistoryReceipt, useSharedHistoryScope } from '../../src/frontend/src/features/shared-projects/SharedWorkbenchHistory';
import { ExternalTaskProgress } from '../../src/frontend/src/features/workbench/ExternalTaskProgress';
import { sharedScopes } from '../../src/frontend/src/features/shared-projects/sharedScopeViews';

it('共享只读回执展示已保存引用状态，个人原进度保留登记动作', () => {
  const receipt = { state: 'done', executor: 'codex', title: '合成任务', external_citations: {
    product_id: 'product-one', project_id: 'shared-one', state: 'pending', ids: ['M1', 'M2'], registered_ids: ['M1'],
  } };
  const shared = renderToStaticMarkup(<SharedHistoryReceipt turn={{ id: 'turn-one', intent: 'do', receipt: { do: receipt } }}/>);
  expect(shared).toContain('待登记 1 个自动引用编号'); expect(shared).toContain('已登记 1 个引用编号');
  expect(shared).not.toContain('<button'); expect(shared).not.toContain('重试引用登记');
  const personal = renderToStaticMarkup(<ExternalTaskProgress receipt={receipt}/>);
  expect(personal).toContain('重新核对'); expect(personal).toContain('重试引用登记');
});

it('正式范围消失或读取拒绝立即隐藏旧正文并撤销旧请求，保留合法退出证明', () => {
  let history;
  const retained = sharedScopes({ items: [{ id: 'shared-one', name: 'shared-one', scope_kind: 'shared', membership: 'unavailable', role: null,
    can_ask: false, can_write: false, can_read_sources: false, has_personal_history: true,
    threads: [{ id: 'thread-one', title: '原问题', updated_at: '2026-11-05T00:00:00Z' }] }] })[0];
  function Probe({ projectScope, scopeError }) {
    history = useSharedHistoryScope('shared-one', projectScope, scopeError, 'thread-one');
    return <div>{history.visible?.turns.map(turn => <p key={turn.id}>{turn.user_text}</p>)}</div>;
  }
  const view = render(<Probe projectScope={retained}/>);
  try {
    expect(history.qualified).toBe(true);
    act(() => history.setState({ scope: history.scope, turns: [{ id: 'turn-one', user_text: '本人原正文' }] }));
    expect(screen.getByText('本人原正文')).toBeTruthy();
    const token = history.gate.issue('history', history.scope);
    view.rerender(<Probe scopeError="当前没有操作权限"/>);
    expect(screen.queryByText('本人原正文')).toBeNull();
    expect(history.gate.isCurrent(token)).toBe(false);
    expect(history.qualified).toBe(false);
  } finally { cleanup(); }
});
