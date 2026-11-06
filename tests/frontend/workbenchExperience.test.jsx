import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { Composer } from '@src/shared/ui/Composer';
import { ContextPanel } from '@src/features/workbench/ContextPanel';

afterEach(cleanup);

it('keeps the automatic action and shows the actual destination for a personal idea', () => {
  render(<Composer projectTag="日常"/>);
  fireEvent.change(screen.getByRole('textbox', { name: '输入' }), { target: { value: '灵感：把待核对原件按日期排列' } });
  expect(screen.getByText('范围 · 收件箱')).toBeInTheDocument();
  expect(screen.getByText('自动判断')).toBeInTheDocument();
  expect(screen.queryByText('会判成记住')).not.toBeInTheDocument();
});

it('keeps shared scope visible and only sends after removing an unsupported project tag', () => {
  const submitted = [];
  render(<Composer projectTag="共享工作" allowedIntents={['ask']} onSend={value => { submitted.push(value.text); return true; }}/>);
  const input = screen.getByRole('textbox', { name: '输入' });
  const send = screen.getByRole('button', { name: '发送' });
  fireEvent.change(input, { target: { value: '#其他项目\n上周核对了哪些资料？' } });
  expect(screen.getByText('范围 · 共享工作')).toBeInTheDocument();
  expect(screen.getByRole('alert')).toHaveTextContent('移除输入里的项目标签');
  expect(send).toBeDisabled();
  fireEvent.keyDown(input, { key: 'Enter' });
  expect(submitted).toEqual([]);
  fireEvent.change(input, { target: { value: '上周核对了哪些资料？' } });
  fireEvent.click(send);
  expect(submitted).toEqual(['上周核对了哪些资料？']);
});

it('replaces a draft and removes its attachments only after the explicit choice', () => {
  const draft = { text: '旧草稿', files: [new File(['原始资料'], '原件.txt')], selection: 'remember' };
  const question = { id: 'cross-project-1', text: '#项目甲\n上周核对了哪些资料？', intent: 'ask', confirmReplace: true };
  const { rerender } = render(<Composer projectTag="项目甲" draft={draft} prefill={question}/>);
  expect(screen.getByRole('textbox', { name: '输入' })).toHaveValue(draft.text);
  expect(screen.getByText('原件.txt')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '保留草稿' }));
  expect(screen.getByText('原件.txt')).toBeInTheDocument();
  rerender(<Composer projectTag="项目甲" draft={draft} prefill={{ ...question, id: 'cross-project-2' }}/>);
  fireEvent.click(screen.getByRole('button', { name: '使用问题' }));
  expect(screen.getByRole('textbox', { name: '输入' })).toHaveValue(question.text);
  expect(screen.queryByText('原件.txt')).not.toBeInTheDocument();
  expect(screen.getByText('问')).toBeInTheDocument();
});

it('hides a draft proposal when navigation invalidates it', () => {
  const draft = { text: '当前输入' };
  const proposal = { id: 'cross-project', text: '原问题？', intent: 'ask', confirmReplace: true };
  const { rerender } = render(<Composer draft={draft} prefill={proposal}/>);
  expect(screen.getByRole('button', { name: '使用问题' })).toBeInTheDocument();
  rerender(<Composer draft={draft}/>);
  expect(screen.queryByRole('button', { name: '使用问题' })).not.toBeInTheDocument();
  expect(screen.getByRole('textbox', { name: '输入' })).toHaveValue(draft.text);
});

it('opens on the selected evidence layer before exposing the technical details', async () => {
  const receipt = { trace: [{ layer: 'source', selected: 1 }], layers: { source: 1 },
    citations: [{ id: 'source-1', n: 1, layer: 'source', title: '会议原件', quote: '逐条核对行动项' }],
    context: { window: 2000, parts: [{ key: 'source', count: 1, tokens: 30 }] } };
  render(<ContextPanel receipt={receipt} activeLayer="source"/>);
  expect(screen.getByText('逐条核对行动项')).toBeVisible();
  expect(screen.getByText('技术详情').closest('details').open).toBe(false);
  await waitFor(() => expect(screen.getByLabelText('原件')).toHaveFocus());
});
