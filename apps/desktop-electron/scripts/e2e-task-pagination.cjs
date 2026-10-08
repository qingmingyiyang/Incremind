const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

// Runs against real admitted DOCX owners. No task repository or renderer API
// is replaced, and no Provider is configured by this gate.
async function runTaskPaginationGate(options) {
  const { temporaryRoot, packageRoot, createDocumentExtractionFixtures,
    enableBuiltinDocumentExtraction, submitWorkspaceFile, waitFor,
    closeWorkspaceSession, openWorkspaceSession, evidenceRoot } = options;
  let session = options.session;
  const count = 51;
  await session.page.evaluate(`(() => {
    localStorage.setItem('chriptmas-os-onboarding-v2-complete', 'true');
    document.querySelector('.first-run-onboarding-close')?.click();
  })()`);
  await enableBuiltinDocumentExtraction(session.page);
  const template = createDocumentExtractionFixtures(temporaryRoot)[0];
  const fixtureRoot = path.join(temporaryRoot, 'pagination-fixtures');
  fs.mkdirSync(fixtureRoot, { recursive: true });
  const generated = spawnSync(path.join(packageRoot, 'resources', 'sidecar', 'runtime', 'python.exe'), ['-c', [
    'import pathlib,sys,zipfile',
    'template,out=pathlib.Path(sys.argv[1]),pathlib.Path(sys.argv[2])',
    'with zipfile.ZipFile(template) as src:',
    ' entries=[(i,src.read(i.filename)) for i in src.infolist()]',
    'for n in range(51):',
    ' with zipfile.ZipFile(out / ("pagination-%03d.docx" % n),"w",zipfile.ZIP_DEFLATED) as dst:',
    '  for info,data in entries:',
    '   if info.filename=="word/document.xml": data=data.replace(b"</w:t>",(" %03d</w:t>" % n).encode(),1)',
    '   dst.writestr(info,data)',
  ].join('\n'), template.filePath, fixtureRoot], { windowsHide: true, encoding: 'utf8', timeout: 30000 });
  if (generated.error || generated.status !== 0) throw new Error('pagination DOCX generation failed');
  const sources = new Set();
  for (let index = 0; index < count; index += 1) {
    const name = `pagination-${String(index).padStart(3, '0')}.docx`;
    const intake = await submitWorkspaceFile(session.page, {
      name, mediaType: template.mediaType, filePath: path.join(fixtureRoot, name),
    });
    if (!intake.source_id || sources.has(intake.source_id)) throw new Error('pagination intake duplicated a source');
    sources.add(intake.source_id);
    if ((index + 1) % 10 === 0) console.log(JSON.stringify({ gate: 'task_pagination', admitted: index + 1 }));
  }
  await session.page.evaluate(`(() => { window.location.hash = '#view=rebuild-settings'; })()`);
  const readPages = async () => session.page.evaluate(`(async () => {
    const base = window.electronAPI.backendBaseUrl + '/api/rebuild/tasks?project_id=default';
    const firstResponse = await fetch(base);
    const first = await firstResponse.json();
    if (!firstResponse.ok || first.items?.length !== 50 || !first.next_cursor) throw new Error('first real task page is incomplete');
    const secondResponse = await fetch(base + '&cursor=' + encodeURIComponent(first.next_cursor));
    const second = await secondResponse.json();
    const items = [...first.items, ...(second.items || [])];
    if (!secondResponse.ok || second.items?.length !== 1 || second.next_cursor
      || new Set(items.map(item => item.task_ref)).size !== 51
      || items.some(item => item.kind !== 'workbench_content_transform' || item.status !== 'delivered')) {
      throw new Error('51 unique delivered DOCX owners are pending: ' + JSON.stringify({
        first_count: first.items.length, second_http: secondResponse.status,
        second_count: second.items?.length, second_has_cursor: Boolean(second.next_cursor),
        unique_count: new Set(items.map(item => item.task_ref)).size,
        statuses: items.reduce((counts, item) => { counts[item.status] = (counts[item.status] || 0) + 1; return counts; }, {}),
        kinds: [...new Set(items.map(item => item.kind))],
        incomplete: items.filter(item => item.status !== 'delivered').map(item => ({ task_ref: item.task_ref, status: item.status })),
      }));
    }
    return { refs: items.map(item => item.task_ref), first_count: first.items.length, second_count: second.items.length };
  })()`);
  const pages = await waitFor(readPages, '51 real delivered task owners', 180000);
  const showPages = async () => {
    await session.page.evaluate(`(() => { window.location.hash = '#view=rebuild-task-center&project_id=default'; })()`);
    await waitFor(async () => session.page.evaluate(`(() => {
      if (document.querySelectorAll('[aria-label="任务列表"] li').length !== 50) throw new Error('task UI first page pending');
      const more = document.querySelector('.task-center-load-more');
      if (!more || more.disabled) throw new Error('task load-more unavailable');
      more.click(); return true;
    })()`), 'task UI first page');
    await waitFor(async () => session.page.evaluate(`(() => {
      if (document.querySelectorAll('[aria-label="任务列表"] li').length !== 51
        || document.querySelector('.task-center-load-more')) throw new Error('task UI second page pending');
      return true;
    })()`), 'task UI second page');
  };
  await showPages();
  await session.page.evaluate(`(() => {
    const filter = [...document.querySelectorAll('[aria-label="任务筛选"] button')].find(node => node.textContent === '进行中');
    if (!filter) throw new Error('active task filter missing');
    filter.click();
  })()`);
  await waitFor(async () => session.page.evaluate(`(() => {
    const clear = document.querySelector('.task-center-clear-filter');
    if (document.querySelectorAll('[aria-label="任务列表"] li').length || !clear) throw new Error('empty active filter pending');
    clear.click(); return true;
  })()`), 'task empty filter recovery');
  await waitFor(async () => session.page.evaluate(`(() => {
    if (document.querySelectorAll('[aria-label="任务列表"] li').length !== 50) throw new Error('filter reset first page pending');
    const more = document.querySelector('.task-center-load-more');
    if (!more || more.disabled) throw new Error('filter reset cursor unavailable');
    more.click(); return true;
  })()`), 'task filter reset pagination');
  await waitFor(async () => session.page.evaluate(`(() => {
    const rows = document.querySelectorAll('[aria-label="任务列表"] li button');
    if (rows.length !== 51) throw new Error('filter reset second page pending');
    rows[50].click(); return true;
  })()`), 'task second-page navigation');
  await waitFor(async () => session.page.evaluate(`(() => {
    if (!window.location.hash.includes(encodeURIComponent(${JSON.stringify(pages.refs[50])}))
      || !document.querySelector('.task-stable-result-list a')) throw new Error('second-page task detail pending');
    return true;
  })()`), 'second-page task detail');
  const detailLayout = await session.page.evaluate(`(() => {
    const width = window.innerWidth;
    const page = document.querySelector('[aria-label="任务详情"]');
    const cards = [...document.querySelectorAll('.task-stable-detail-grid > article')];
    const overflow = Math.max(0, document.documentElement.scrollWidth - width);
    if (!page || cards.length !== 4 || overflow > 1
      || page.getBoundingClientRect().right > width + 1
      || cards.some(card => card.getBoundingClientRect().right > width + 1)) {
      throw new Error('real task detail overflows viewport: ' + JSON.stringify({ width, overflow }));
    }
    return { width, horizontal_overflow: overflow, visible_cards: cards.length };
  })()`);
  if (evidenceRoot) {
    fs.mkdirSync(evidenceRoot, { recursive: true });
    const screenshot = await session.page.send('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(path.join(evidenceRoot, 'pagination-task-detail.png'), Buffer.from(screenshot.data, 'base64'));
  }
  await closeWorkspaceSession(session);
  session = await openWorkspaceSession(temporaryRoot);
  // Expose the new session immediately so the parent can always clean it up,
  // including when a post-restart assertion throws.
  options.onSessionChanged(session);
  const restart = await waitFor(readPages, '51 task owners after restart');
  if (JSON.stringify(restart.refs) !== JSON.stringify(pages.refs)) throw new Error('restart changed the stable task order');
  await showPages();
  return { status: 'passed', gate: 'task_pagination', admitted_sources: sources.size,
    first_page: pages.first_count, second_page: pages.second_count,
    unique_tasks: pages.refs.length, ui_load_more: true, empty_filter_recovery: true,
    second_page_detail: true, detail_layout: detailLayout, restart_order_and_ui: true, controlled_docx: true,
    isolated_app_data: true, native_picker_verified: false, live_provider_verified: false };
}

module.exports = { runTaskPaginationGate };
