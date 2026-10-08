import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Workbench } from '@src/features/workbench/Workbench';
const receipt = {
 answer: '保留具体数字。', no_match: false,
 citations: [{ n: 3, layer: 'source', persona: false, id: 'source-1', title: '原文标题', quote: '先用数字\n…\n再给依据', locator: { coordinate_space: 'source_content_v1', windows: [{ start: 0, end: 4 }, { start: 100, end: 104 }] } }],
 layers: { insight: 2, summary: 1, note: 0, source: 1, persona: 2 },
 trace: [{ layer: 'insight', considered: 2, selected: 2, stopped: false }, { layer: 'summary', considered: 1, selected: 1, stopped: false }, { layer: 'source', considered: 1, selected: 1, stopped: true }],
};
const turn = { id: 'ask-1', thread_id: 'thread-1', intent: 'ask', user_text: '怎么写？', receipt: { ask: receipt } };
let turns;
beforeEach(() => { localStorage.clear(); turns = [turn]; vi.stubGlobal('fetch', vi.fn(async url => ({ ok: true, json: async () => String(url).includes('/threads/') ? { id: 'thread-1', turns } : String(url).endsWith('/turns') ? { thread_id: 'thread-1', turn } : { items: [{ id: 'thread-1', title: '写作' }] } }))); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
it('renders the actual answer and original citation numbers with sent layer counts', async () => {
 render(<Workbench projectId="alpha"/>); await act(async () => {});
 expect(screen.getByText('保留具体数字。')).toBeInTheDocument(); expect(screen.getByLabelText('引用 3 · 原件 · 原文标题')).toHaveTextContent('3');
 expect(screen.getByText('认 2')).toBeInTheDocument(); expect(screen.getByText('摘 1')).toBeInTheDocument(); expect(screen.getByText('原 1')).toBeInTheDocument(); expect(screen.getByText('我 2')).toBeInTheDocument(); expect(screen.queryByText('整 0')).not.toBeInTheDocument();
 expect(screen.queryByRole('button', { name: /引用 3/ })).not.toBeInTheDocument();
});
it('opens the trace with actual citations and sent persona count even when persona is not cited', async () => {
 render(<Workbench projectId="alpha"/>); await act(async () => {}); fireEvent.click(screen.getByRole('button', { name: '原件 1 · 本次用了什么' }));
 const panel = screen.getByRole('dialog', { name: '上下文' });
 fireEvent.click(within(panel).getByText('阶梯'));
 expect(within(panel).getAllByText('原文标题')).toHaveLength(2); expect(within(panel).getByText('先用数字 … 再给依据')).toBeInTheDocument(); expect(within(panel).getByTitle('已足够')).toBeInTheDocument();
 expect(within(within(panel).getByRole('group', { name: '画像' })).getByText('2')).toBeInTheDocument();
 expect(within(panel).getAllByRole('button', { name: /原文标题/ })).toHaveLength(2);
 fireEvent.click(within(panel).getByRole('button', { name: '关闭' })); expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});
it('keeps an ask receipt intact when confirming an older inspiration', async () => {
 turns = [turn, { id: 'inspiration-1', intent: 'inspiration', user_text: '灵感', receipt: { inspiration: { insight: { id: 'pending-1', state: 'pending', text: '数字', revision: 1 } } } }];
 fetch.mockImplementation(async url => ({ ok: true, json: async () => String(url).includes('/confirm') ? { id: 'active-1', state: 'active', text: '数字', revision: 2 } : String(url).includes('/threads/') ? { turns } : { items: [{ id: 'thread-1', title: '写作' }] } }));
 render(<Workbench projectId="alpha"/>); await act(async () => {}); fireEvent.click(screen.getByRole('button', { name: '确认' })); await act(async () => {});
 expect(screen.getByText('保留具体数字。')).toBeInTheDocument();
});
it('submits a synchronous ask turn without a preview request', async () => {
 fetch.mockImplementation(async url => ({ ok: true, json: async () => String(url).endsWith('/turns') ? { thread_id: 'thread-1', turn } : { items: [] } }));
 render(<Workbench projectId="alpha"/>); await act(async () => {});
 fireEvent.change(screen.getByRole('textbox'), { target: { value: '怎么写？' } }); fireEvent.click(screen.getByRole('button', { name: '发送' })); await act(async () => {});
 expect(JSON.parse(fetch.mock.calls.find(([url]) => String(url).endsWith('/turns'))[1].body)).toEqual({ project_id: 'alpha', text: '怎么写？', intent: 'ask' });
 expect(screen.getByText('保留具体数字。')).toBeInTheDocument(); expect(fetch.mock.calls.some(([url]) => String(url).includes('/preview'))).toBe(false);
});
it('closes the old trace when starting a new conversation', async () => {
 render(<Workbench projectId="alpha"/>); await act(async () => {}); fireEvent.click(screen.getByRole('button', { name: '摘要 1 · 本次用了什么' }));
 fireEvent.click(screen.getByRole('button', { name: '新对话' })); expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});
it('renders a no-match answer without invented layer badges or citations', async () => {
 turns = [{ ...turn, receipt: { ask: { answer: '没有匹配资料。', citations: [], layers: { insight: 0, summary: 0, note: 0, source: 0, persona: 0 }, trace: [], no_match: true } } }];
 render(<Workbench projectId="alpha"/>); await act(async () => {});
 expect(screen.getByText('没有匹配资料。')).toBeInTheDocument(); expect(screen.queryByLabelText(/引用/)).not.toBeInTheDocument();
 expect(screen.queryByRole('button', { name: / · 本次用了什么/ })).not.toBeInTheDocument();
});
it('hides an open trace immediately when the project changes', async () => {
 const app = render(<Workbench projectId="alpha"/>); await act(async () => {}); fireEvent.click(screen.getByRole('button', { name: '摘要 1 · 本次用了什么' }));
 expect(screen.getByRole('dialog')).toBeInTheDocument();
 app.rerender(<Workbench projectId="beta"/>); expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

it('drills a context citation through the project-scoped library read model', async () => {
 const original = fetch.getMockImplementation();
 fetch.mockImplementation(async (url, options) => String(url).includes('/library/drill?')
  ? { ok: true, json: async () => ({ source: { title: '真实原件', window: { pre: '前文', quote: '真实证据', post: '后文' } } }) }
  : original(url, options));
 render(<Workbench projectId="alpha"/>); await act(async () => {});
 fireEvent.click(screen.getByRole('button', { name: '原件 1 · 本次用了什么' }));
 fireEvent.click(screen.getByText('条目'));
 fireEvent.click(within(screen.getByLabelText('条目')).getByRole('button', { name: /原文标题/ })); await act(async () => {});
 expect(fetch.mock.calls.some(([url]) => String(url).includes('project_id=alpha') && String(url).includes('from=source') && String(url).includes('id=source-1'))).toBe(true);
 expect(screen.getByRole('dialog', { name: '原件' })).toHaveTextContent('真实证据');
 fireEvent.click(screen.getByRole('button', { name: '关闭' }));
 expect(screen.getByRole('dialog', { name: '上下文' })).toBeInTheDocument();
});

it('opens task context without inventing a budget or an ask ladder', async () => {
 turns = [{ id: 'do-1', intent: 'do', user_text: '写一份提纲', receipt: { do: { task_id: 'task-1', title: '提纲', state: 'waiting_approval', progress: { done: 1, total: 3 } } } }];
 render(<Workbench projectId="alpha"/>); await act(async () => {});
 fireEvent.click(screen.getByRole('button', { name: '本次上下文' }));
 const panel = screen.getByRole('dialog', { name: '上下文' });
 expect(within(panel).getByLabelText('总量')).toHaveTextContent('— / — —');
 expect(within(panel).queryByText('阶梯')).not.toBeInTheDocument();
});

it('discards a late context drill when the project changes', async () => {
 const original = fetch.getMockImplementation(); let finish;
 fetch.mockImplementation((url, options) => String(url).includes('/library/drill?')
  ? new Promise(resolve => { finish = () => resolve({ ok: true, json: async () => ({ source: { title: '旧项目', window: { quote: '迟到的正文' } } }) }); })
  : original(url, options));
 const app = render(<Workbench projectId="alpha"/>); await act(async () => {});
 fireEvent.click(screen.getByRole('button', { name: '原件 1 · 本次用了什么' }));
 fireEvent.click(screen.getByText('条目'));
 fireEvent.click(within(screen.getByLabelText('条目')).getByRole('button', { name: /原文标题/ }));
 expect(screen.getByRole('dialog', { name: '原件' })).toBeInTheDocument();
 app.rerender(<Workbench projectId="beta"/>); await act(async () => { finish(); });
 expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
 expect(screen.queryByText('迟到的正文')).not.toBeInTheDocument();
});
