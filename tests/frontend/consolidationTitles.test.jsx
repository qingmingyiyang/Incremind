import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterAll, afterEach, beforeAll, expect, it } from 'vitest';
import { spawn } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { ConsolidationSuggestions } from '@src/features/library/ConsolidationSuggestions';

const repo = path.resolve(process.cwd(), '../..');
const rootParent = path.join(repo, 'work/O0-root');
mkdirSync(rootParent, { recursive: true });
const root = mkdtempSync(path.join(rootParent, 'http-'));
const base = 'http://127.0.0.1:8090';
const titles = ['缓存按需加载与检索结果复用的实施记录和后续核对事项', '请求范围隔离记录'];
const originalElectron = globalThis.electronAPI;
let server, output = '', insight, persona, documentIds;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

beforeAll(async () => {
  const python = path.join(repo, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
  server = spawn(python, [path.join(repo, 'tests/frontend/consolidation_http_server.py'), root, '8090'], {
    cwd: repo, env: { ...process.env, PYTHONPATH: [repo, path.join(repo, 'src')].join(path.delimiter),
      PYTHONUNBUFFERED: '1', CHRIPTMAS_APP_ROOT: root }, windowsHide: true,
  });
  server.stdout.on('data', data => { output += data; });
  server.stderr.on('data', data => { output += data; });
  const deadline = Date.now() + 60000;
  let rows;
  while (Date.now() < deadline && server.exitCode === null) {
    try {
      const response = await fetch(base + '/api/recognition/restructure-proposals?project_id=alpha');
      if (response.ok) { rows = (await response.json()).items; break; }
    } catch { /* 等待真实服务开始监听。 */ }
    await sleep(100);
  }
  if (!rows?.length) throw new Error('Consolidation HTTP server did not start: ' + output);
  insight = rows[0].target_recognition_ids[0];
  documentIds = rows[0].snapshot.experiences.flatMap(row => row.payload.provenance.source_refs.map(ref => ref.id));
  persona = JSON.parse(readFileSync(path.join(root, 'identities.json'), 'utf8')).persona;
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

async function assertDocumentButtons(opened) {
  for (let index = 0; index < titles.length; index += 1) {
    const button = screen.getByRole('button', { name: '打开整理稿 ' + titles[index] });
    expect(button).toHaveAttribute('title', titles[index]);
    expect(button).toHaveTextContent(titles[index]);
    fireEvent.click(button);
  }
  expect(opened).toEqual(documentIds.map(identity => [identity, 'alpha']));
  for (const identity of documentIds) expect(document.body.textContent).not.toContain(identity);
  expect(document.body.textContent).not.toMatch(/\bdoc(?:ument)?-/);
}

it('shows merge source titles and opens the original document through actual HTTP', async () => {
  const opened = [];
  render(<ConsolidationSuggestions projectId="alpha" insightId={insight}
    onOpenDocument={(...args) => opened.push(args)}/>);
  fireEvent.click(await screen.findByRole('button', { name: '合并 1' }));
  await assertDocumentButtons(opened);
});

it('shows support source titles through the original project with actual HTTP', async () => {
  const opened = [];
  render(<ConsolidationSuggestions projectId="me" insightId={persona}
    onOpenDocument={(...args) => opened.push(args)}/>);
  fireEvent.click(await screen.findByRole('button', { name: '支持 1' }));
  await assertDocumentButtons(opened);
});
