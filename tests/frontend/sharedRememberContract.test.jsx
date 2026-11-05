import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { expect, it } from 'vitest';
import { sharedRememberRequest } from '../../src/frontend/src/features/workbench/workbenchScope';
import { sharedRememberRecovery, sharedRememberResult } from '../../src/frontend/src/features/shared-projects/useSharedRemember';
import { SharedHistoryReceipt } from '../../src/frontend/src/features/shared-projects/SharedWorkbenchHistory';

it('共享文字记住使用正式范围和原线程，未入库及失权回执不能打开来源', () => {
  const request = sharedRememberRequest('project-one', '会议记录', [], 'thread-one');
  expect(request).toEqual({ project_id: 'project-one', scope_kind: 'shared', intent: 'remember', text: '会议记录', thread_id: 'thread-one' });
  expect(() => sharedRememberRequest('project-one', '#other 会议记录', [])).toThrow();
  expect(sharedRememberRecovery({ state: 'preparing', recoverable: false }, request)).toBe(false);
  expect(sharedRememberRecovery({ state: 'failed', recoverable: true, request: { ...request } }, request)).toBe(true);
  expect(() => sharedRememberRecovery({ state: 'preparing', recoverable: true,
    request: { ...request, text: '后来输入' } }, request)).toThrow();
  const memory = { item_id: 'item-one', state: 'processing', title: '会议记录', document_id: null,
    progress: { done: 2, total: 4 }, sources_available: false, insights: [], related: [], verified: false, error: null };
  const turn = { id: 'turn-one', scope_kind: 'shared', thread_id: 'thread-one', project_id: 'project-one',
    intent: 'remember', receipt: { remember: memory } };
  const accepted = { thread_id: 'thread-one', turn };
  expect(sharedRememberResult(accepted, 'project-one', 'thread-one')).toBe(accepted);
  expect(() => sharedRememberResult(accepted, 'project-one', 'another-thread')).toThrow();
  expect(() => sharedRememberResult({ ...accepted, turn: { ...turn, receipt: { remember: { ...memory, state: 'done' } } } },
    'project-one', 'thread-one')).toThrow();
  const rendered = renderToStaticMarkup(<SharedHistoryReceipt turn={turn}/>);
  expect(rendered).toContain('data-progress="running"');
  expect(rendered).not.toContain('打开整理稿');
  expect(rendered).not.toContain('打开原件');
  const retained = { ...turn, receipt: { remember: { ...memory, state: 'done', document_id: 'document-one',
    sources_available: true, insights: [{ id: 'insight-one', text: '共享认识正文', state: 'pending' }] } } };
  const lostAccess = renderToStaticMarkup(<SharedHistoryReceipt turn={retained} rememberActions={{ canRead: false }}/>);
  expect(lostAccess).not.toContain('共享认识正文');
  expect(lostAccess).not.toContain('打开整理稿');
});
