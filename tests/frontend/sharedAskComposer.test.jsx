import React from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { expect, it } from 'vitest';
import { Composer } from '../../src/frontend/src/shared/ui/Composer';
import { sharedAskRequest } from '../../src/frontend/src/features/workbench/workbenchScope';
import { readScopedTurn } from '../../src/frontend/src/shared/lib/threadHistory';

it('共享问答沿正式请求字段且拒绝附件、跨标签和错误返回范围', () => {
  expect(sharedAskRequest('shared-one', '合成短问', [], 'ask')).toEqual({ project_id: 'shared-one', scope_kind: 'shared',
    text: '合成短问', intent: 'ask', executor: 'builtin' });
  expect(() => sharedAskRequest('shared-one', '#other\n合成短问', [], 'ask')).toThrow('顶部选择');
  expect(() => sharedAskRequest('shared-one', '合成短问', [new File(['合成资料'], '材料.txt')], 'ask')).toThrow('当前可提问');
  expect(() => readScopedTurn({ id: 'turn-one', scope_kind: 'personal' }, 'shared')).toThrow();
  expect(readScopedTurn({ id: 'turn-one', scope_kind: 'shared' }, 'shared').id).toBe('turn-one');
});

it('固定共享ASK保留正文并关闭动作、附件入口，个人默认入口保持可用', () => {
  const view = render(<Composer fixedIntent="ask" allowFiles={false} draft={{ text: '合成短问', selection: 'do' }}/>);
  try {
    expect(screen.getByRole('textbox', { name: '输入' }).value).toBe('合成短问');
    expect(screen.getByRole('button', { name: '添加文件' }).disabled).toBe(true);
    expect(screen.getByRole('button', { name: '选择动作' }).disabled).toBe(true);
    expect(screen.queryByRole('button', { name: '改回自动判断' })).toBeNull();
    fireEvent.change(screen.getByRole('textbox', { name: '输入' }), { target: { value: '#other\n合成短问' } });
    expect(screen.getByRole('textbox', { name: '输入' }).value).toBe('#other\n合成短问');
    expect(screen.queryByRole('menu')).toBeNull();
    view.unmount(); render(<Composer/>);
    expect(screen.getByRole('button', { name: '添加文件' }).disabled).toBe(false);
    expect(screen.getByRole('button', { name: '选择动作' }).disabled).toBe(false);
  } finally { cleanup(); }
});
