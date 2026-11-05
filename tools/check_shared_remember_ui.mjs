import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

const root = fileURLToPath(new URL('../src/frontend', import.meta.url));
const require = createRequire(path.join(root, 'package.json'));
const { transformWithOxc } = await import(pathToFileURL(require.resolve('vite')).href);
const entries = ['features/workbench/Workbench.jsx', 'features/workbench/workbenchApi.js',
  'features/workbench/workbenchScope.js', 'features/shared-projects/useSharedRemember.js',
  'features/shared-projects/SharedWorkbenchHistory.jsx'];
for (const entry of entries) {
  const file = path.join(root, 'src', entry);
  assert((await transformWithOxc(await readFile(file, 'utf8'), file)).code);
}
console.log(`Vite OXC modified-module checks: ${entries.length} passed; no server or listener`);
