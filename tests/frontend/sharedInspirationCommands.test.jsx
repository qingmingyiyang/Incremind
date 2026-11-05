import React from 'react';
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { newSharedInspirationCommand, markSharedInspirationCommand, sharedInspirationCommands, sharedInspirationResult } from '../../src/frontend/src/features/shared-projects/sharedInspirationCommands';
import { sharedInspirationRequest } from '../../src/frontend/src/features/workbench/workbenchScope';
import { SharedInspirationReceipts } from '../../src/frontend/src/features/shared-projects/SharedInspirationReceipts';
import { Composer } from '../../src/frontend/src/shared/ui/Composer';

afterEach(() => { cleanup(); localStorage.clear(); });

describe('共享灵感原命令和能力', () => {
  it('真实Storage保存原请求，未知与未受理继续沿原编号和正文，范围独立', () => {
    const command = newSharedInspirationCommand('caller:target:device:shared:project:inspiration', 'same-project', '人工灵感原文', 'original-command');
    markSharedInspirationCommand(command, 'unknown');
    const [cold] = sharedInspirationCommands(command.storageKey, command.projectId);
    expect(cold.id).toBe(command.id); expect(cold.body).toEqual({ text: '人工灵感原文' }); expect(cold.phase).toBe('unknown');
    markSharedInspirationCommand(cold, 'not_received');
    const [retry] = sharedInspirationCommands(command.storageKey, command.projectId);
    expect(retry.id).toBe(command.id); expect(retry.body).toEqual(command.body); expect(retry.phase).toBe('not_received');
    expect(() => markSharedInspirationCommand({ ...retry, body: { text: '另一份正文' } }, 'unknown')).toThrow();
    expect(sharedInspirationCommands('caller:other-target:device:shared:project:inspiration', command.projectId)).toEqual([]);
    expect(sharedInspirationCommands('caller:target:other-device:shared:project:inspiration', command.projectId)).toEqual([]);
    expect(sharedInspirationCommands('caller:target:device:personal:project:inspiration', command.projectId)).toEqual([]);
  });
  it('正式pending DTO保持人审状态，错命令和错逻辑范围拒绝', () => {
    const command = newSharedInspirationCommand('scope-one', 'same-project', '真实输入形状', 'dto-command');
    const result = { command_id: command.id, state: 'accepted', project_id: command.projectId,
      scope_kind: 'shared', namespace: 'actual-server-namespace', kind: 'inspiration',
      insight: { id: 'candidate-one', revision: 1, state: 'pending', content: '待人工确认' } };
    expect(sharedInspirationResult(result, command)).toBe(result);
    expect(() => sharedInspirationResult({ ...result, command_id: 'other-command' }, command)).toThrow();
    expect(() => sharedInspirationResult({ ...result, scope_kind: 'personal' }, command)).toThrow();
    expect(() => sharedInspirationResult({ ...result, namespace: null }, command)).toThrow();
    expect(sharedInspirationRequest('same-project', command.body.text, [])).toEqual(command.body);
    expect(() => sharedInspirationRequest('same-project', '#其他项目 内容', [])).toThrow();
    expect(() => sharedInspirationRequest('same-project', '内容', [{}])).toThrow();
    render(<SharedInspirationReceipts projectId="same-project" canWrite state={{ commands: [command], values: { [command.id]: result } }}/>);
    expect(screen.getByText('待确认')).toBeTruthy();
    expect(screen.queryByRole('button', { name: '确认' })).toBeNull();
  });
  it('原Composer只呈现允许的共享动作，移除写权限后清除旧选中动作的执行资格', () => {
    const view = render(<Composer allowFiles={false} allowedIntents={['ask', 'inspiration']}/>);
    fireEvent.click(screen.getByRole('button', { name: '选择动作' }));
    expect(screen.getAllByRole('menuitem')).toHaveLength(2);
    fireEvent.click(screen.getByRole('menuitem', { name: /灵感/ }));
    expect(screen.getByText('灵感')).toBeTruthy();
    expect(screen.queryByRole('button', { name: '改回自动判断' })).toBeNull();
    view.rerender(<Composer allowFiles={false} fixedIntent="ask" allowedIntents={['ask']}/>);
    expect(screen.queryByText('灵感')).toBeNull();
    expect(screen.getByText('问')).toBeTruthy();
    expect(screen.getByRole('button', { name: '选择动作' }).disabled).toBe(true);
  });
});
