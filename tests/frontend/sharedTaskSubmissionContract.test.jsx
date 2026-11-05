import { act, renderHook } from '@testing-library/react';
import { expect, it } from 'vitest';
import { forgetDeviceCredential, saveDeviceCredential, selectUserSpace } from '../../src/frontend/src/shared/api/deviceTransport';
import { sharedTaskAcceptance, sharedTaskCommandResult, sharedTaskNotReceived, useSharedTaskSubmission } from '../../src/frontend/src/features/shared-projects/useSharedTaskSubmission';
import { workbenchSubmission } from '../../src/frontend/src/features/workbench/workbenchApi';
import { sharedTaskRequest } from '../../src/frontend/src/features/workbench/workbenchScope';

it('共享干活的原命令独立保留，同编号恢复只认原线程与实际受理轮次', () => {
  saveDeviceCredential({ key: 'a'.repeat(43), device: { device_id: 'device-one', user_id: 'actor-one' } });
  const opts = { enabled: true, projectId: 'project-one' };
  const body = { project_id: opts.projectId, scope_kind: 'shared', intent: 'do', executor: 'builtin',
    text: '原任务正文', thread_id: 'thread-one' };
  try {
    const limit = '𠮷'.repeat(60_000);
    expect(sharedTaskRequest(opts.projectId, limit, []).text).toBe(limit);
    expect(() => sharedTaskRequest(opts.projectId, limit + '字', [])).toThrow('60,000');
    const first = renderHook(() => useSharedTaskSubmission(opts));
    let command;
    act(() => { command = first.result.current.prepare(body); });
    const ask = workbenchSubmission({ ...body, intent: 'ask', text: '另一条问答' });
    act(() => { expect(first.result.current.prepare({ ...body, text: '新任务草稿' })).toBe(null); });
    expect(first.result.current.visible.entry).toMatchObject({ command: { id: command.id, body }, accepted: null });
    const started = { thread_id: body.thread_id, turn: { id: 'accepted-turn', intent: 'do', scope_kind: 'shared' } };
    expect(() => sharedTaskAcceptance(command, { ...started, thread_id: 'other-thread' })).toThrow();
    const status = { command_id: command.id, project_id: opts.projectId, scope_kind: 'shared' };
    expect(sharedTaskCommandResult(command, { ...status, state: 'preparing' })).toMatchObject({ state: 'preparing' });
    expect(sharedTaskCommandResult(command, { ...status, state: 'unknown' })).toMatchObject({ state: 'unknown' });
    expect(sharedTaskCommandResult(command, { ...status, state: 'failed', error: 'model_not_configured' }))
      .toEqual({ state: 'failed', error: 'model_not_configured' });
    expect(sharedTaskCommandResult(command, { ...status, state: 'accepted', thread_id: body.thread_id, turn_id: 'accepted-turn' }))
      .toEqual({ state: 'accepted', accepted: { threadId: body.thread_id, turnId: 'accepted-turn' } });
    expect(() => sharedTaskCommandResult(command, { ...status, project_id: 'other-project', state: 'unknown' })).toThrow();
    expect(sharedTaskNotReceived({ code: 'shared_task_not_received', status: 404 })).toBe(true);
    expect(sharedTaskNotReceived({ code: 'shared_task_not_received', status: 409 })).toBe(false);
    expect(sharedTaskNotReceived({ code: 'shared_task_command_unavailable', status: 404 })).toBe(false);
    act(() => { first.result.current.observe(command, 'not_received'); });
    act(() => { first.result.current.refresh(); });
    expect(first.result.current.visible.entry).toMatchObject({ status: 'not_received', command: { id: command.id, body } });
    act(() => { first.result.current.acknowledge(command, sharedTaskAcceptance(command, started)); });
    first.unmount();
    selectUserSpace({ user_id: 'target-two' });
    const restored = renderHook(() => useSharedTaskSubmission(opts));
    expect(restored.result.current.visible.entry).toBe(null);
    act(() => { selectUserSpace(null); restored.rerender(); });
    expect(restored.result.current.visible.entry).toMatchObject({
      command: { id: command.id, body }, accepted: { threadId: 'thread-one', turnId: 'accepted-turn' },
    });
    act(() => { restored.result.current.settle(restored.result.current.visible.entry.command); });
    expect(restored.result.current.visible.entry).toBe(null);
    const localValues = Array.from({ length: localStorage.length }, (_, index) => localStorage.getItem(localStorage.key(index)));
    expect(localValues.some(value => value?.includes(body.text))).toBe(false);
    ask.clear();
    restored.unmount();
  } finally { forgetDeviceCredential(); }
});
