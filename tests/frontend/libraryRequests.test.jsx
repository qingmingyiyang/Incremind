import { cleanup, fireEvent, render, screen, waitFor, act } from '@testing-library/react';
import { afterAll, afterEach, beforeAll, expect, it } from 'vitest';
import { spawn } from 'node:child_process';
import { mkdtempSync, mkdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { Library } from '@src/features/library/Library';

const repo = path.resolve(process.cwd(), '../..');
mkdirSync(path.join(repo, 'work'), { recursive: true });
const root = mkdtempSync(path.join(repo, 'work/R4-http-'));
const base = 'http://127.0.0.1:8014';
let server, output = '';
const originalElectron = globalThis.electronAPI;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const requests = () => readFileSync(path.join(root, 'requests.jsonl'), 'utf8').trim().split('\n').filter(Boolean).map(JSON.parse);
const lists = () => requests().filter(row => /^\/api\/v2\/library\/(insights|summaries|notes|sources)$/.test(row.path));
const layerRequests = layer => lists().filter(row => row.path.endsWith('/' + layer) && row.query.project_id === 'alpha');

beforeAll(async () => {
  const python = process.platform === 'win32' ? path.join(repo, '.venv/Scripts/python.exe') : path.join(repo, '.venv/bin/python');
  server = spawn(python, [path.join(repo, 'tests/frontend/library_http_server.py'), root, '8014'], {
    cwd: repo, env: { ...process.env, PYTHONPATH: path.join(repo, 'src'), PYTHONUNBUFFERED: '1' }, windowsHide: true,
  });
  server.stdout.on('data', data => { output += data; });
  server.stderr.on('data', data => { output += data; });
  const deadline = Date.now() + 90000;
  let ready = false;
  while (Date.now() < deadline && server.exitCode === null) {
    try { ready = (await fetch(base + '/api/health')).ok; } catch { /* 等待真实服务开始监听。 */ }
    if (ready) break;
    await sleep(100);
  }
  if (!ready) throw new Error('Library HTTP server did not start: ' + output);
  globalThis.electronAPI = { backendBaseUrl: base };
}, 100000);
afterEach(() => cleanup());
afterAll(async () => {
  globalThis.electronAPI = originalElectron;
  if (server && server.exitCode === null) {
    server.kill();
    await new Promise(resolve => server.once('exit', resolve));
  }
});

it('debounces searches and waits for Chinese composition using actual HTTP and SQLite', async () => {
  render(<Library projectId="alpha" initialLayer="source"/>);
  await screen.findByRole('button', { name: 'Alpha original' });
  const before = layerRequests('sources').length;
  const search = screen.getByRole('searchbox');
  fireEvent.change(search, { target: { value: 'a' } });
  fireEvent.change(search, { target: { value: 'al' } });
  fireEvent.change(search, { target: { value: 'alpha' } });
  await screen.findByRole('button', { name: 'Alpha original' });
  expect(layerRequests('sources').slice(before).map(row => row.query.q)).toEqual(['alpha']);
  fireEvent.compositionStart(search);
  fireEvent.change(search, { target: { value: '搜' } });
  await act(() => sleep(350));
  expect(layerRequests('sources').length).toBe(before + 1);
  fireEvent.change(search, { target: { value: '搜索' } });
  fireEvent.compositionEnd(search);
  await screen.findByRole('button', { name: 'Alpha original' });
  expect(layerRequests('sources').slice(before).map(row => row.query.q)).toEqual(['alpha', '搜索']);
});

it('loads only the selected layer and requests archives only for forgotten notes', async () => {
  const start = lists().length;
  const archivesBefore = requests().filter(row => row.path.endsWith('/documents-archived')).length;
  render(<Library projectId="alpha" initialLayer="source"/>);
  await screen.findByRole('button', { name: 'Alpha original' });
  await act(() => sleep(350));
  expect(lists().slice(start).filter(row => row.query.project_id === 'alpha').map(row => row.path)).toEqual(['/api/v2/library/sources']);
  expect(screen.getByRole('button', { name: '摘要 —' })).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '整理稿 —' }));
  await screen.findByRole('button', { name: '整理稿 0' });
  expect(requests().filter(row => row.path.endsWith('/documents-archived'))).toHaveLength(archivesBefore);
  fireEvent.click(screen.getByRole('button', { name: '已遗忘 0' }));
  await waitFor(() => expect(requests().filter(row => row.path.endsWith('/documents-archived'))).toHaveLength(archivesBefore + 1));
  fireEvent.click(screen.getByRole('button', { name: '摘要 —' }));
  await screen.findByRole('button', { name: '摘要 0' });
  expect(lists().slice(start).filter(row => row.query.project_id === 'alpha').map(row => row.path))
    .toEqual(['/api/v2/library/sources', '/api/v2/library/notes', '/api/v2/library/summaries']);
});

it('discards a slow original-list response after switching the current layer', async () => {
  render(<Library projectId="alpha" initialLayer="source"/>);
  await screen.findByRole('button', { name: 'Alpha original' });
  const before = layerRequests('sources').length;
  fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'slow' } });
  await waitFor(() => expect(layerRequests('sources').length).toBe(before + 1));
  fireEvent.click(screen.getByRole('button', { name: '整理稿 —' }));
  await screen.findByRole('button', { name: '整理稿 0' });
  await act(() => sleep(600));
  expect(screen.getByRole('button', { name: '整理稿 0' })).toHaveAttribute('aria-pressed', 'true');
  expect(screen.getByRole('button', { name: '原件 —' })).toBeInTheDocument();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});
