import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

const root = fileURLToPath(new URL('../src/frontend', import.meta.url));
const require = createRequire(path.join(root, 'package.json'));
const { transformWithOxc } = await import(pathToFileURL(require.resolve('vite')).href);
const entries = ['AppRouter.jsx', 'features/library/Library.jsx', 'features/library/LibraryOriginals.jsx',
  'features/library/InsightReviewQueue.jsx', 'features/library/SharedLibraryView.jsx', 'features/library/sharedLibraryApi.js',
  'features/library/LibraryInsightEditor.jsx', 'features/library/DraftNavigation.jsx', 'features/library/SharedSupersession.jsx',
  'features/shared-projects/SharedProjectsPanel.jsx', 'features/library/LibraryTools.jsx',
  'features/library/SharedActiveMerge.jsx'];
for (const entry of entries) {
  const file = path.join(root, 'src', entry);
  assert((await transformWithOxc(await readFile(file, 'utf8'), file)).code);
}
console.log(`Vite OXC modified-module checks: ${entries.length} passed; no server or listener`);
