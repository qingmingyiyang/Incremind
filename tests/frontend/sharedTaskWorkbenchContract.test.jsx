import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { expect, it } from 'vitest';
import { SharedHistoryReceipt } from '../../src/frontend/src/features/shared-projects/SharedWorkbenchHistory';
import { sharedScopes } from '../../src/frontend/src/features/shared-projects/sharedScopeViews';
import { workbenchApi } from '../../src/frontend/src/features/workbench/workbenchApi';
import { sharedTaskOutcome, sharedTaskRedoRevision, sharedTaskRequest } from '../../src/frontend/src/features/workbench/workbenchScope';
import { forgetDeviceCredential, saveDeviceCredential, selectUserSpace, userStorageKey } from '../../src/frontend/src/shared/api/deviceTransport';

it('共享干活使用正式范围并独立核对原执行与本人交付成果', () => {
  expect(sharedTaskRequest('project-one', '整理计划', [])).toEqual({
    project_id: 'project-one', scope_kind: 'shared', intent: 'do', executor: 'builtin', text: '整理计划',
  });
  expect(() => sharedTaskRequest('project-one', '#other 整理计划', [])).toThrow();
  const scope = { id: 'project-one', name: '小组', scope_kind: 'shared', membership: 'active', role: 'viewer',
    can_ask: true, can_do: true, can_write: false, can_read_sources: true, has_personal_history: false, threads: [] };
  expect(sharedScopes({ items: [scope] })[0].can_do).toBe(true);
  expect(() => sharedScopes({ items: [{ ...scope, membership: 'unavailable', role: null, can_ask: false, can_read_sources: false }] })).toThrow();
  expect(sharedTaskRedoRevision({ source_kind: 'execution', revision: 3, items: null })).toBe(3);
  expect(() => sharedTaskRedoRevision({ source_kind: 'sample', revision: 3 })).toThrow();
  const document = { id: 'document-one', project_id: scope.id, title: '整理结果', revision: 1, markdown: '已交付正文' };
  const result = { scope_kind: 'shared', turn_id: 'turn-one', document };
  expect(sharedTaskOutcome(result, scope.id, 'turn-one', document.id)).toBe(document);
  expect(() => sharedTaskOutcome(result, 'another-project', 'turn-one', document.id)).toThrow();
  const task = { state: 'done', title: '整理计划', kernel_turn_id: 'kernel-one', document_id: document.id,
    progress: { done: 3, total: 3 }, division: [{ assignment_id: 'step-one', goal: '核对范围', state: 'done' }] };
  const turn = { id: 'turn-one', scope_kind: 'shared', intent: 'do', receipt: { do: task } };
  const history = renderToStaticMarkup(<SharedHistoryReceipt turn={turn} taskActions={{ projectId: scope.id, canRead: false }}/>);
  expect(history).toContain('打开成果');
  expect(history).toContain('核对范围');
  expect(history).not.toContain('重做');
  expect(history).not.toContain('删除样例');
});

it('刷新读取原重做编号和旧格式正文，待定回执只显示核对', () => {
  saveDeviceCredential({ key: 'a'.repeat(43), device: { device_id: 'device-one', user_id: 'actor-one' } });
  const projectId = 'project-one';
  const turn = { id: 'turn-one', thread_id: 'thread-one', scope_kind: 'shared', intent: 'do',
    receipt: { do: { state: 'done', title: '原任务', kernel_turn_id: 'kernel-one', document_id: 'draft-one' } } };
  const actions = { projectId, canDo: true };
  const slot = userStorageKey('chriptmas-v2-request:shared:project-one:redo:turn-one:device:device-one');
  const old = { project_id: projectId, expected_revision: 3, source_kind: 'execution', scope_kind: 'shared' };
  try {
    sessionStorage.setItem(slot, JSON.stringify({ id: 'original-key', body: JSON.stringify(old) }));
    const command = workbenchApi.pendingSharedRedo(projectId, turn.id);
    expect(command).toMatchObject({ id: 'original-key', body: { project_id: projectId, expected_revision: 3, source_kind: 'execution' } });
    expect(command.body).not.toHaveProperty('scope_kind');
    expect(() => workbenchApi.prepareSharedRedo(projectId, turn.id, 4)).toThrow();
    const markup = renderToStaticMarkup(<SharedHistoryReceipt turn={turn} taskActions={actions}/>);
    expect(markup).toContain('核对原重做');
    expect(markup).not.toContain('继续原重做');
    expect(markup).not.toContain('>重做</button>');
    selectUserSpace({ user_id: 'target-two' });
    expect(workbenchApi.pendingSharedRedo(projectId, turn.id)).toBeNull();
    expect(() => workbenchApi.settleSharedRedo(command)).toThrow();
    selectUserSpace(null);
    workbenchApi.settleSharedRedo(command);
    expect(workbenchApi.pendingSharedRedo(projectId, turn.id)).toBeNull();
  } finally {
    sessionStorage.removeItem(slot);
    forgetDeviceCredential();
  }
});
