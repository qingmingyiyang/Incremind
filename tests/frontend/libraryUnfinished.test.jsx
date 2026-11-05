import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterAll, afterEach, beforeAll, expect, it } from 'vitest';
import { spawn } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { Library } from '@src/features/library/Library';
import { Workbench } from '@src/features/workbench/Workbench';

const repo = path.resolve(process.cwd(), '../..');
const parent = path.join(repo, 'work/O2-root');
mkdirSync(parent, { recursive: true });
const root = mkdtempSync(path.join(parent, 'http-'));
const base = 'http://127.0.0.1:8091';
const originalElectron = globalThis.electronAPI;
let server, output = '';
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

beforeAll(async () => {
  const python = path.join(repo, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
  server = spawn(python, [path.join(repo, 'tests/frontend/unfinished_http_server.py'), root, '8091'], {
    cwd: repo, env: { ...process.env, PYTHONPATH: [repo, path.join(repo, 'src')].join(path.delimiter),
      PYTHONUNBUFFERED: '1', CHRIPTMAS_APP_ROOT: root }, windowsHide: true,
  });
  server.stdout.on('data', data => { output += data; });
  server.stderr.on('data', data => { output += data; });
  let ready = false;
  const deadline = Date.now() + 60000;
  while (Date.now() < deadline && server.exitCode === null) {
    try { ready = (await fetch(base + '/api/v2/library/sources?project_id=alpha')).ok; } catch { /* 等待真实服务监听。 */ }
    if (ready) break;
    await sleep(100);
  }
  if (!ready) throw new Error('Unfinished originals HTTP server did not start: ' + output);
  globalThis.electronAPI = { backendBaseUrl: base };
}, 70000);
afterEach(() => cleanup());
afterAll(async () => {
  globalThis.electronAPI = originalElectron;
  if (server && server.exitCode === null) {
    server.kill();
    await new Promise(resolve => server.once('exit', resolve));
  }
});

it('opens the original directly from a failed receipt through actual HTTP', async () => {
  render(<Workbench projectId="alpha" threadId="unfinished-turn"/>);
  const open = await screen.findByRole('button', { name: '打开原件' });
  expect(open.closest('.workbench-receipt-title')).toBeTruthy();
  fireEvent.click(open);
  await screen.findByText('原文内容保持完整', { selector: '.ui-source-text' });
});

it('retries the failed original through the existing endpoint and keeps counts separate', async () => {
  render(<Library projectId="alpha" initialLayer="source"/>);
  const original = await screen.findByRole('button', { name: '未完成原件' });
  const row = original.closest('.ui-row');
  expect(row.querySelector('.ui-status-dot')).toHaveAttribute('data-state', 'failed');
  expect(screen.getByRole('button', { name: '原件 0' })).toBeInTheDocument();
  const processing = screen.getByRole('button', { name: '处理中原件' }).closest('.ui-row');
  expect(within(processing).getByRole('button', { name: '重试' })).toBeDisabled();
  const retry = within(row).getByRole('button', { name: '重试' });
  fireEvent.click(retry);
  expect(retry).toBeDisabled();
  fireEvent.click(retry);
  await waitFor(() => expect(screen.getByRole('button', { name: '原件 1' })).toBeInTheDocument());
  const ordinary = screen.getByRole('button', { name: '未完成原件' }).closest('.ui-row');
  expect(within(ordinary).queryByRole('button', { name: '重试' })).not.toBeInTheDocument();
  const response = await fetch(base + '/api/v2/library/sources?project_id=alpha');
  expect((await response.json()).items.find(item => item.id === 'unfinished-original').state).toBeUndefined();
  expect(readFileSync(path.join(root, 'retries.jsonl'), 'utf8').trim().split('\n').map(JSON.parse))
    .toEqual([{ path: '/api/v2/workbench/turns/unfinished-turn/retry' }]);
});
