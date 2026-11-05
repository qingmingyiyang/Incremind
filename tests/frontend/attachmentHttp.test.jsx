import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterAll, afterEach, beforeAll, expect, it } from 'vitest';
import { spawn } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync, unlinkSync, existsSync, appendFileSync } from 'node:fs';
import path from 'node:path';
import { File as NativeFile } from 'node:buffer';
import { createRequire } from 'node:module';
import { Workbench } from '@src/features/workbench/Workbench';

const repo = path.resolve(process.cwd(), '../..');
const { FormData: NativeFormData, fetch: nativeFetch } = createRequire(path.join(repo, 'src/frontend/package.json'))('undici');
const parent = path.join(repo, 'work/O1-root');
mkdirSync(parent, { recursive: true });
const root = mkdtempSync(path.join(parent, 'http-'));
const base = 'http://127.0.0.1:8092';
const originalElectron = globalThis.electronAPI;
const originalFormData = globalThis.FormData;
const originalFetch = globalThis.fetch;
let server, output = '';
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const requests = () => existsSync(path.join(root, 'requests.jsonl'))
  ? readFileSync(path.join(root, 'requests.jsonl'), 'utf8').trim().split('\n').map(JSON.parse) : [];

async function start() {
  server = spawn(path.join(repo, '.venv/Scripts/python.exe'),
    [path.join(repo, 'tests/frontend/attachment_http_server.py'), root, '8092'], {
      cwd: repo, env: { ...process.env, PYTHONPATH: [repo, path.join(repo, 'src')].join(path.delimiter) }, windowsHide: true,
    });
  server.stdout.on('data', data => { output += data; appendFileSync(path.join(root, 'server.log'), data); });
  server.stderr.on('data', data => { output += data; appendFileSync(path.join(root, 'server.log'), data); });
  const deadline = Date.now() + 60000;
  while (Date.now() < deadline && server.exitCode === null) {
    try { if ((await fetch(base + '/api/v2/library/sources?project_id=alpha')).ok) return; } catch { /* 等待真实监听。 */ }
    await sleep(100);
  }
  throw new Error('Attachment HTTP server did not start: ' + output);
}
async function stop() {
  if (server && server.exitCode === null) {
    await fetch(base + '/test/server/stop', { method: 'POST' });
    await new Promise(resolve => server.once('exit', resolve));
  }
}
beforeAll(async () => { await start(); globalThis.electronAPI = { backendBaseUrl: base }; globalThis.FormData = NativeFormData; globalThis.fetch = nativeFetch; }, 70000);
afterEach(() => cleanup());
afterAll(async () => { globalThis.electronAPI = originalElectron; globalThis.FormData = originalFormData; await stop(); globalThis.fetch = originalFetch; });

async function compose(text, count) {
  const view = render(<Workbench projectId="alpha"/>);
  await waitFor(() => expect(screen.getByRole('textbox', { name: '输入' })).toBeEnabled());
  const input = view.container.querySelector('input[type=file]');
  fireEvent.change(input, { target: { files: Array.from({ length: count }, (_, index) =>
    new NativeFile(['预算十万元。'], `访谈${index}.txt`, { type: 'text/plain' })) } });
  fireEvent.change(screen.getByRole('textbox', { name: '输入' }), { target: { value: text } });
  fireEvent.click(screen.getByRole('button', { name: '发送' }));
}

it('uploads three attachments and submits one automatic turn through real HTTP', async () => {
  const before = requests().length;
  await compose('对比三份的分歧', 3);
  await waitFor(() => expect(screen.getByRole('textbox', { name: '输入' })).toHaveValue(''), { timeout: 20000 });
  const rows = requests().slice(before);
  expect(rows.filter(row => row.path.endsWith('/files'))).toHaveLength(3);
  const turns = rows.filter(row => row.body);
  expect(turns).toHaveLength(1);
  expect(turns[0].body.item_ids).toHaveLength(3);
  expect(turns[0].body.intent).toBeUndefined();
}, 25000);

it('keeps file-only submissions on the original remember path', async () => {
  const before = requests().length;
  await compose('', 2);
  await waitFor(() => expect(screen.queryByRole('button', { name: '移除 访谈1.txt' })).not.toBeInTheDocument(), { timeout: 20000 });
  const turns = requests().slice(before).filter(row => row.body);
  expect(turns).toHaveLength(2);
  expect(turns.every(row => row.body.intent === 'remember' && row.body.item_id && !row.body.item_ids)).toBe(true);
}, 25000);

it('reuses the uploaded single attachment after an actual server disconnect', async () => {
  const before = requests().length;
  const signal = path.join(root, 'disconnect-next-turn');
  writeFileSync(signal, 'disconnect');
  await compose('整理成纪要', 1);
  await waitFor(() => expect(requests().slice(before).filter(row => row.body)).toHaveLength(1), { timeout: 20000 });
  await stop();
  unlinkSync(signal);
  await waitFor(() => expect(screen.getByRole('button', { name: '发送' })).toBeEnabled(), { timeout: 20000 });
  await start();
  fireEvent.click(screen.getByRole('button', { name: '发送' }));
  await waitFor(() => expect(screen.getByRole('textbox', { name: '输入' })).toHaveValue(''), { timeout: 20000 });
  const rows = requests().slice(before);
  expect(rows.filter(row => row.path.endsWith('/files'))).toHaveLength(1);
  const turns = rows.filter(row => row.body);
  expect(turns).toHaveLength(2);
  expect(turns[1].body.item_ids).toEqual(turns[0].body.item_ids);
}, 70000);
