import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Workbench } from '@src/features/workbench/Workbench';

const projects = [{ id: 'alpha', name: '厨房', scenes: [] }, { id: 'beta', name: '旅行', scenes: ['日本'] }];
const turn = { id: 'old-ask', intent: 'ask', user_text: '机票怎么安排？', receipt: { ask: {
  answer: '原回答不变。', citations: [], layers: { insight: 1 }, trace: [],
  elsewhere: { project_id: 'beta', scene: null },
} } };
let current, finish;
beforeEach(() => {
  localStorage.clear(); current = structuredClone(turn); finish = null;
  vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
    if (String(url).endsWith('/turns')) {
      const result = { thread_id: 'new-beta', turn: { id: 'new-ask', intent: 'ask', user_text: '机票怎么安排？', receipt: { ask: { answer: '目标回答。', citations: [], layers: {}, trace: [] } } } };
      return finish ? new Promise(resolve => { finish = () => resolve({ ok: true, json: async () => result }); }) : { ok: true, json: async () => result };
    }
    return { ok: true, json: async () => String(url).includes('/threads/old-thread')
      ? { id: 'old-thread', turns: [current] } : { items: String(url).includes('project_id=alpha')
        ? [{ id: 'old-thread', title: '原问题' }] : [] } };
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const props = { projectId: 'alpha', threadId: 'old-thread', projects };
const load = async options => { const app = render(<Workbench {...props} {...options}/>); await act(async () => {}); return app; };

it('uses the original new-turn endpoint with a target ID and the same question', async () => {
  const navigate = vi.fn(); await load({ onNavigate: navigate });
  const button = screen.getByRole('button', { name: '在 #旅行 里问' });
  expect(button).toHaveTextContent('→ #旅行');
  expect(button.closest('.workbench-ask-layers')).toHaveTextContent('认 1');
  fireEvent.click(button); await act(async () => {});
  const requests = fetch.mock.calls.filter(([url]) => String(url).endsWith('/turns'));
  expect(requests).toHaveLength(1);
  expect(JSON.parse(requests[0][1].body)).toEqual({ project_id: 'beta', text: '#beta\n机票怎么安排？', intent: 'ask' });
  expect(navigate).toHaveBeenCalledWith('workbench', { project_id: 'beta', thread_id: 'new-beta' });
  expect(screen.getByText('原回答不变。')).toBeInTheDocument();
  expect(screen.queryByText('目标回答。')).not.toBeInTheDocument();
});

it('preserves the suggested scene in the explicit target tag', async () => {
  current.receipt.ask.elsewhere.scene = '日本'; await load();
  fireEvent.click(screen.getByRole('button', { name: '在 #旅行/日本 里问' })); await act(async () => {});
  expect(JSON.parse(fetch.mock.calls.find(([url]) => String(url).endsWith('/turns'))[1].body)).toEqual({
    project_id: 'beta', text: '#beta/日本\n机票怎么安排？', intent: 'ask',
  });
});

it('uses only the target ID when a scene cannot fit the existing tag grammar', async () => {
  current.receipt.ask.elsewhere.scene = '日本 机票'; await load();
  fireEvent.click(screen.getByRole('button', { name: '在 #旅行 里问' })); await act(async () => {});
  expect(JSON.parse(fetch.mock.calls.find(([url]) => String(url).endsWith('/turns'))[1].body)).toEqual({
    project_id: 'beta', text: '#beta\n机票怎么安排？', intent: 'ask',
  });
});

it('refuses an unavailable target before POST and retains the original answer', async () => {
  current.receipt.ask.elsewhere.project_id = 'removed'; await load();
  fireEvent.click(screen.getByRole('button', { name: '在 #removed 里问' })); await act(async () => {});
  expect(screen.getByRole('alert')).toHaveTextContent('项目未找到');
  expect(fetch.mock.calls.filter(([url]) => String(url).endsWith('/turns'))).toHaveLength(0);
  expect(screen.getByText('原回答不变。')).toBeInTheDocument();
});

it('does not replay a hint request while busy and suppresses a late response after switching threads', async () => {
  finish = () => {}; const navigate = vi.fn(); const app = await load({ onNavigate: navigate });
  const button = screen.getByRole('button', { name: '在 #旅行 里问' });
  fireEvent.click(button); fireEvent.click(button);
  expect(fetch.mock.calls.filter(([url]) => String(url).endsWith('/turns'))).toHaveLength(1);
  app.rerender(<Workbench {...props} threadId="another-thread" onNavigate={navigate}/>);
  await act(async () => { finish(); });
  expect(navigate).not.toHaveBeenCalled();
  expect(screen.queryByText('目标回答。')).not.toBeInTheDocument();
  expect(localStorage.getItem('chriptmas-v2-thread:beta')).toBeNull();
});

it('keeps an ordinary receipt unchanged when no hint exists', async () => {
  delete current.receipt.ask.elsewhere; await load();
  expect(screen.queryByRole('button', { name: /里问/ })).not.toBeInTheDocument();
  expect(screen.getByText('原回答不变。')).toBeInTheDocument();
  expect(screen.getByText('认 1')).toBeInTheDocument();
  expect(fetch.mock.calls.filter(([url]) => String(url).endsWith('/turns'))).toHaveLength(0);
});

it('never offers the ask hint on a Do receipt', async () => {
  current = { ...current, intent: 'do', receipt: { do: { title: '提纲', state: 'running', progress: { done: 0, total: 1 }, elsewhere: { project_id: 'beta', scene: null } } } };
  await load();
  expect(screen.queryByRole('button', { name: /里问/ })).not.toBeInTheDocument();
  expect(fetch.mock.calls.filter(([url]) => String(url).endsWith('/turns'))).toHaveLength(0);
});
