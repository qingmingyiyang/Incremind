import { useState } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Workbench } from '@src/features/workbench/Workbench';
import { DeviceGate } from '@src/features/settings/DeviceGate';
import { forgetDeviceCredential, saveDeviceCredential, selectUserSpace, userStorageKey } from '@src/shared/api/deviceTransport';

const userA = { user_id: 'user-a', name: '甲' }, userB = { user_id: 'user-b', name: '乙' };
const slot = 'chriptmas-v2-thread:default';
const response = value => ({ ok: true, status: 200, json: async () => value });
const encode = (event, value) => new TextEncoder().encode(`event: ${event}\ndata: ${JSON.stringify(value)}\n\n`);
let wire, controller, withThreads;

function RoutedWorkbench() {
  const [threadId, setThreadId] = useState();
  return <Workbench projectId="default" threadId={threadId}
    onNavigate={(_view, destination) => setThreadId(destination.thread_id)}/>;
}

beforeEach(() => {
  localStorage.clear(); sessionStorage.clear(); forgetDeviceCredential();
  history.replaceState(null, '', '/');
  saveDeviceCredential({ key: 'u'.repeat(43), device: { device_id: 'admin-device', user_id: 'local-user' } });
  withThreads = true; controller = null;
  wire = vi.fn(async (input, options = {}) => {
    const path = new URL(String(input), location.href).pathname;
    const target = new Headers(options.headers).get('X-Chriptmas-Target-User');
    const name = target === userA.user_id ? '甲' : '乙';
    if (path.endsWith('/devices')) return response({ mode: 'server', items: [] });
    if (path.endsWith('/threads')) return response({ items: withThreads
      ? [{ id: `thread-${target}-first`, title: `${name}第一段` }, { id: `thread-${target}-last`, title: `${name}第二段` }] : [] });
    if (path.includes('/threads/')) {
      const id = path.split('/').at(-1);
      const text = id.endsWith('-last') ? `${name}选择后的正文` : `${name}首次的正文`;
      return response({ id, turns: [{ id: `turn-${id}`, thread_id: id, intent: 'remember', user_text: text,
        receipt: { remember: { title: `${name}材料`, state: 'done', progress: { done: 4, total: 4 }, insights: [], related: [] } } }] });
    }
    if (path.endsWith('/turns')) return { ok: true, status: 200, headers: new Headers({ 'Content-Type': 'text/event-stream' }),
      body: new ReadableStream({ start(value) { controller = value; } }) };
    return response({ items: [] });
  });
  vi.stubGlobal('fetch', wire);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); forgetDeviceCredential(); history.replaceState(null, '', '/'); });

const detailCalls = target => wire.mock.calls.filter(([input, options]) =>
  new URL(String(input), location.href).pathname.includes('/threads/')
  && new Headers(options.headers).get('X-Chriptmas-Target-User') === target);

it('restores each actual selected thread after the device gate switches A to B and back to A', async () => {
  selectUserSpace(userA);
  const slotA = userStorageKey(slot);
  render(<DeviceGate><RoutedWorkbench/></DeviceGate>);
  await screen.findByText('甲首次的正文');
  fireEvent.change(screen.getByRole('combobox', { name: '对话' }), { target: { value: 'thread-user-a-last' } });
  await screen.findByText('甲选择后的正文');
  act(() => selectUserSpace(userB));
  const slotB = userStorageKey(slot);
  await screen.findByText('乙首次的正文');
  fireEvent.change(screen.getByRole('combobox', { name: '对话' }), { target: { value: 'thread-user-b-last' } });
  await screen.findByText('乙选择后的正文');
  act(() => selectUserSpace(userA));
  await waitFor(() => expect(detailCalls(userA.user_id)).toHaveLength(3));
  expect(new URL(String(detailCalls(userA.user_id).at(-1)[0]), location.href).pathname)
    .toBe('/api/v2/workbench/threads/thread-user-a-last');
  expect(screen.getByRole('combobox', { name: '对话' })).toHaveValue('thread-user-a-last');
  expect(await screen.findByText('甲选择后的正文')).toBeInTheDocument();
  expect(screen.queryByText('乙选择后的正文')).toBeNull();
  expect(JSON.parse(localStorage.getItem(slotA))).toBe('thread-user-a-last');
  expect(JSON.parse(localStorage.getItem(slotB))).toBe('thread-user-b-last');
  expect(localStorage.getItem(slot)).toBeNull();
});

it('writes the actual started and completed answer thread only in the selected user cache', async () => {
  withThreads = false;
  selectUserSpace(userA);
  const slotA = userStorageKey(slot);
  render(<DeviceGate><Workbench projectId="default"/></DeviceGate>);
  await screen.findByText('今天想记住什么？');
  fireEvent.change(screen.getByRole('textbox'), { target: { value: '怎么写？' } });
  fireEvent.click(screen.getByRole('button', { name: '发送' }));
  await waitFor(() => expect(controller).not.toBeNull());
  const started = { thread_id: 'thread-user-a-new', turn: { id: 'turn-user-a-new', intent: 'ask', user_text: '怎么写？' } };
  await act(async () => { controller.enqueue(encode('started', started)); });
  expect(JSON.parse(localStorage.getItem(slotA))).toBe(started.thread_id);
  expect(localStorage.getItem(slot)).toBeNull();
  // 清掉本次开始回执的记录，分别验证终态回执确实写回同一用户槽。
  localStorage.removeItem(slotA);
  await act(async () => {
    controller.enqueue(encode('done', { ...started, turn: { ...started.turn,
      receipt: { ask: { answer: '甲的完整回答。', citations: [],
        layers: { insight: 0, summary: 0, note: 0, source: 0, persona: 0 }, trace: [] } } } }));
    controller.close();
  });
  expect(await screen.findByText('甲的完整回答。')).toBeInTheDocument();
  expect(JSON.parse(localStorage.getItem(slotA))).toBe(started.thread_id);
  act(() => selectUserSpace(userB));
  const slotB = userStorageKey(slot);
  await screen.findByText('今天想记住什么？');
  expect(localStorage.getItem(slotB)).toBeNull();
  expect(JSON.parse(localStorage.getItem(slotA))).toBe(started.thread_id);
  expect(localStorage.getItem(slot)).toBeNull();
});
