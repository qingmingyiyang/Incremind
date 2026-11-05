import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';
import { ownedSharedBackups, sharedBackupImport } from '../src/frontend/src/features/shared-projects/sharedBackupViews.js';
import { newSharedBackupCommand, markSharedBackupCommand, sharedBackupCommands } from '../src/frontend/src/features/shared-projects/sharedBackupCommands.js';

const root = fileURLToPath(new URL('../src/frontend', import.meta.url));
const require = createRequire(path.join(root, 'package.json'));
const { JSDOM } = require('jsdom');
if (!process.argv[2]) throw new Error('Actual retained backend DTO JSON path required');
const actual = JSON.parse(await readFile(process.argv[2], 'utf8'));
const realm = new JSDOM('', { url: 'https://shared-backup.invalid/' });
const original = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: realm.window.localStorage });
try {
  assert(localStorage instanceof realm.window.Storage);
  const [backup] = ownedSharedBackups(actual.owned);
  const imported = sharedBackupImport(actual.restore, { backup: backup.command_id });
  assert.equal(backup.state, 'ready'); assert.equal(imported.state, 'completed');
  assert.equal(imported.requires_resume, false);
  const key = `shared-backup-mechanism:${crypto.randomUUID()}`;
  const saved = newSharedBackupCommand('restore', backup.command_id, backup.project_name, key, imported.name);
  assert.deepEqual(saved.body, { name: imported.name, approved: true, batch_size: 8 });
  markSharedBackupCommand(saved, 'unknown');
  const [cold] = sharedBackupCommands(key);
  assert.equal(cold.id, saved.id); assert.equal(cold.phase, 'unknown');
  assert.equal(cold.path, `/backups/${encodeURIComponent(backup.command_id)}/projects`);
  assert.deepEqual(cold.body, saved.body);
  markSharedBackupCommand(cold, 'not_received');
  const [retry] = sharedBackupCommands(key);
  assert.equal(retry.id, saved.id); assert.deepEqual(retry.body, saved.body);
  assert.throws(() => markSharedBackupCommand({ ...retry, body: { ...retry.body, name: '另一个请求' } }, 'unknown'));
  assert.deepEqual(sharedBackupCommands(`${key}:another-user`), []);
  assert.throws(() => sharedBackupImport(actual.restore, { backup: 'another-backup' }));
  assert.throws(() => ownedSharedBackups({ items: [{ ...backup, scope: 'personal-runtime' }] }));
  console.log('Actual retained backend DTO and jsdom Storage command mechanism: passed');
} finally {
  if (original) Object.defineProperty(globalThis, 'localStorage', original);
  else delete globalThis.localStorage;
  realm.window.close();
}
const { transformWithOxc } = await import(pathToFileURL(require.resolve('vite')).href);
const postcss = require('postcss');
const entries = ['SharedBackupPanel.jsx', 'SharedProjectManagement.jsx', 'SharedProjectsPanel.jsx',
  'sharedBackupApi.js', 'sharedBackupCommands.js', 'sharedBackupViews.js', 'sharedProjects.css'];
for (const entry of entries) {
  const file = path.join(root, 'src/features/shared-projects', entry), code = await readFile(file, 'utf8');
  if (entry.endsWith('.css')) postcss.parse(code, { from: file });
  else assert((await transformWithOxc(code, file)).code);
}
console.log(`Vite OXC and PostCSS modified-module checks: ${entries.length} passed; no server or listener`);
