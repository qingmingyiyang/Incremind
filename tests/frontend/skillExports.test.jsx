import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import SettingsPage from '@src/features/settings/SettingsPage';
import { Library } from '@src/features/library/Library';

const methods = [
  { id: 'recognition-one', revision: 3, text: '先整理证据，再作判断', conditions: ['作出判断前'], scene: '阅读' },
  { id: 'recognition-two', revision: 4, text: '检查结论的反例', conditions: ['已有结论时'], scene: null },
];
const document = { name: 'evidence-check', description: '需要检验结论时使用', trigger: '作出判断前',
  steps: [{ text: '整理证据', sources: [1] }, { text: '检查反例', sources: [2] }], validation: ['每项结论有证据'] };
const originalDraft = { id: 'skill-one', project_id: 'alpha', scene: '阅读', sources: methods.map((row, i) => ({ ...row, number: i + 1 })),
  document, revision: 7, reviewed: false, needs_update: false, exported_revision: null, document_version: 1 };
let draft, requests, eligible, listed, delay, click, createUrl, revokeUrl, generation, folder;
const copy = value => JSON.parse(JSON.stringify(value));
const settle = async () => act(async () => {});
const response = (value, status = 200) => ({ ok: status < 400, status, json: async () => copy(value), headers: new Headers(), blob: async () => new Blob(['PK-synthetic-zip'], { type: 'application/zip' }) });
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
beforeEach(() => {
  draft = copy(originalDraft); requests = []; eligible = copy(methods); listed = true; delay = null;
  generation = true; folder = { available: false, confirmed: false, revision: 0 };
  createUrl = vi.fn(() => 'blob:skill-export'); revokeUrl = vi.fn();
  class BrowserURL extends URL {}
  BrowserURL.createObjectURL = createUrl; BrowserURL.revokeObjectURL = revokeUrl; vi.stubGlobal('URL', BrowserURL);
  click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
    const path = new URL(String(url), 'http://localhost');
    const method = options.method || 'GET', body = options.body ? JSON.parse(options.body) : null;
    const record = { path: path.pathname, query: path.searchParams, method, body }; requests.push(record);
    if (delay?.match(record)) return delay.promise;
    if (path.pathname.includes('/skill-exports')) {
      if (path.pathname.endsWith('/methods')) return response({ items: eligible.filter(row => row.scene === null || row.scene === path.searchParams.get('scene')) });
      if (path.pathname.endsWith('/skill-exports') && method === 'GET') return response({ items: listed ? [draft] : [], generation_available: generation, local_folder: folder });
      if (path.pathname.endsWith('/download')) {
        if (body.expected_revision !== draft.revision || !draft.reviewed || draft.needs_update) return response({ detail: 'skill_review_required' }, 409);
        draft = { ...draft, revision: draft.revision + 1, exported_revision: draft.document_version };
        return response(null);
      }
      if (path.pathname.endsWith('/review')) { draft = { ...draft, revision: draft.revision + 1, reviewed: true }; return response(draft); }
      if (path.pathname.endsWith('/folder')) {
        folder = { available: true, confirmed: true, revision: 1 };
        draft = { ...draft, revision: draft.revision + 1, folder_path: body.directory + '/evidence-check' }; return response(draft);
      }
      if (path.pathname.endsWith('/generate') || path.pathname.endsWith('/regenerate')) {
        const generated = copy(document); generated.steps = generated.steps.map(step => ({ ...step, sources: [Math.min(step.sources[0], body.sources.length)] }));
        draft = { ...draft, sources: body.sources.map((source, i) => ({ ...eligible.find(row => row.id === source.id), ...source, number: i + 1 })), document: body.document || generated, revision: draft.revision + 1, reviewed: false, needs_update: false };
        return response(draft);
      }
      if (method === 'PATCH' || method === 'POST') {
        draft = { ...draft, document: body.document, ...(body.sources ? { sources: body.sources.map((source, i) => ({ ...eligible.find(row => row.id === source.id), ...source, number: i + 1 })), scene: body.scene } : {}), revision: draft.revision + 1, reviewed: false };
        listed = true; return response(draft);
      }
      return response(draft);
    }
    if (path.pathname.endsWith('/settings')) return response({ model: {}, privacy: {} });
    if (path.pathname.endsWith('/projects')) return response({ items: [{ id: 'alpha', name: '阅读项目', scenes: ['阅读', '写作'], revision: 1 }, { id: 'beta', name: '另一项目', scenes: [], revision: 1 }] });
    if (path.pathname.endsWith('/drill')) return response({ insight: { ...methods[0], kind: 'recognition', state: 'active', source_count: 1, related: [], document_ids: [] }, grown: [] });
    if (path.pathname.endsWith('/insights')) return response({ items: [{ ...methods[0], kind: 'recognition', state: 'active', source_count: 1 }], counts: { active: 1 } });
    if (path.pathname.endsWith('/consolidate')) return response({ score: 0, limit: false, running: false, job_id: null });
    return response({ items: [] });
  }));
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
async function settings() { const view = render(<SettingsPage projectId="alpha" initialSection="project" expandProject="alpha"/>); await settle(); return view; }
async function list() { const view = await settings(); fireEvent.click(screen.getByRole('button', { name: '导出为 skill', exact: true })); await settle(); return view; }
async function openDraft() { const view = await list(); fireEvent.click(screen.getByRole('button', { name: document.name, exact: true })); await settle(); return view; }
const skillCalls = ending => requests.filter(row => row.path.endsWith(ending));

it('opens project exports and exposes the real needs-update state', async () => {
  draft.needs_update = true;
  await list();
  expect(screen.getByText('需更新')).toBeInTheDocument();
  expect(screen.getByText('需更新')).toHaveStyle({ color: 'var(--red)', fontFamily: 'var(--mono)', fontSize: '11px' });
  fireEvent.click(screen.getByRole('button', { name: document.name })); await settle();
  expect(screen.getByRole('button', { name: '下载 ZIP' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '审阅' })).toBeDisabled();
});

it('checks backend method eligibility on the insight menu before composing', async () => {
  eligible = [methods[1]];
  render(<Library projectId="alpha" projects={[{ id: 'alpha', name: '阅读项目', scenes: ['阅读'] }]}/>); await settle();
  fireEvent.click(screen.getByRole('button', { name: methods[0].text })); await settle();
  fireEvent.click(screen.getByRole('button', { name: '更多', exact: true }));
  fireEvent.click(screen.getByRole('button', { name: '导出为 skill', exact: true })); await settle();
  expect(skillCalls('/methods')[0].query.get('scene')).toBe('阅读');
  expect(screen.getByRole('alert')).toHaveTextContent('认识已变化');
  expect(screen.queryByLabelText('名称')).not.toBeInTheDocument();
  expect(requests.filter(row => row.method === 'POST' && row.path.includes('/skill-exports'))).toHaveLength(0);
});

it('creates a manual draft with numbered step sources and no model call', async () => {
  listed = false; await list();
  fireEvent.change(screen.getByLabelText('skill 场景'), { target: { value: '阅读' } });
  fireEvent.click(screen.getByRole('button', { name: '新建草稿' })); await settle();
  fireEvent.click(screen.getByRole('checkbox', { name: methods[0].text }));
  for (const [label, text] of [['名称', 'manual-method'], ['什么时候用', '判断前使用'], ['触发边界', '证据已收齐'], ['步骤 1', '核对证据'], ['验证规则', '每条有出处']]) {
    fireEvent.change(within(screen.getByRole('dialog')).getByRole('textbox', { name: label, exact: true }), { target: { value: text } });
  }
  expect(screen.getByRole('checkbox', { name: '步骤 1 来源 1' })).toBeChecked();
  fireEvent.click(screen.getByRole('checkbox', { name: '步骤 1 来源 1' }));
  expect(screen.getByRole('checkbox', { name: '步骤 1 来源 1' })).toBeChecked();
  fireEvent.click(screen.getByRole('button', { name: '保存草稿' })); await settle();
  const created = requests.find(row => row.method === 'POST' && row.path.endsWith('/skill-exports'));
  expect(created.body.sources).toEqual([{ id: methods[0].id, revision: 3 }]);
  expect(created.body.scene).toBe('阅读');
  expect(created.body.document.steps).toEqual([{ text: '核对证据', sources: [1] }]);
  expect(skillCalls('/generate')).toHaveLength(0);
  expect(screen.getByRole('button', { name: '下载 ZIP' })).toBeDisabled();
  fireEvent.click(screen.getByText('连接说明'));
  expect(screen.getByText('.agents/skills/manual-method/SKILL.md')).toBeInTheDocument();
  expect(screen.getByText('.claude/skills/manual-method/SKILL.md')).toBeInTheDocument();
});

it('edits, explicitly reviews, and downloads twice using refreshed CAS revisions', async () => {
  draft.reviewed = true; await openDraft();
  fireEvent.change(screen.getByRole('textbox', { name: '步骤 1', exact: true }), { target: { value: '先查证据' } });
  expect(screen.getByRole('button', { name: '下载 ZIP' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '保存草稿' })); await settle();
  expect(draft.reviewed).toBe(false);
  expect(screen.getByRole('button', { name: '下载 ZIP' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '审阅' })); await settle();
  fireEvent.click(screen.getByRole('button', { name: '下载 ZIP' })); await settle();
  expect(click).toHaveBeenCalledTimes(1); expect(createUrl.mock.calls[0][0]).toBeInstanceOf(Blob);
  expect(skillCalls('/download')[0].body).toEqual({ expected_revision: 9 });
  fireEvent.click(screen.getByRole('button', { name: '下载 ZIP' })); await settle();
  expect(skillCalls('/download')[1].body).toEqual({ expected_revision: 10 });
  expect(click).toHaveBeenCalledTimes(2);
});

it('regenerates with current eligible revisions and still requires review', async () => {
  draft.needs_update = true; eligible[0].revision = 6; await openDraft();
  fireEvent.click(screen.getByRole('button', { name: '重新生成' })); await settle();
  expect(skillCalls('/regenerate')[0].body.sources).toEqual([{ id: methods[0].id, revision: 6 }, { id: methods[1].id, revision: 4 }]);
  expect(skillCalls('/regenerate')[0].body.expected_revision).toBe(7);
  expect(skillCalls('/regenerate')[0].body.key).toEqual(expect.any(String));
  expect(skillCalls('/regenerate')[0].body).not.toHaveProperty('document');
  expect(screen.getByRole('button', { name: '下载 ZIP' })).toBeDisabled();
});

it('does not save a delayed ZIP after closing the panel', async () => {
  draft.reviewed = true; await openDraft();
  delay = { ...deferred(), match: row => row.path.endsWith('/download') };
  fireEvent.click(screen.getByRole('button', { name: '下载 ZIP' }));
  fireEvent.click(screen.getByRole('button', { name: '关闭', exact: true }));
  await act(async () => delay.resolve(response(null)));
  expect(click).not.toHaveBeenCalled(); expect(createUrl).not.toHaveBeenCalled();
});

it('does not reopen a delayed old draft after project scope changes', async () => {
  const view = await list(); delay = { ...deferred(), match: row => row.path.endsWith('/skill-one') };
  fireEvent.click(screen.getByRole('button', { name: document.name }));
  view.rerender(<SettingsPage projectId="beta" initialSection="project" expandProject="beta"/>); await settle();
  await act(async () => delay.resolve(response(draft)));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

it('serializes double review clicks and preserves an edit on CAS failure', async () => {
  await openDraft(); delay = { ...deferred(), match: row => row.path.endsWith('/review') };
  fireEvent.click(screen.getByRole('button', { name: '审阅' })); fireEvent.click(screen.getByRole('button', { name: '审阅' }));
  expect(skillCalls('/review')).toHaveLength(1);
  await act(async () => delay.resolve(response({ detail: 'skill_revision_conflict' }, 409)));
  expect(screen.getByRole('alert')).toHaveTextContent('内容已变化');
  expect(screen.getByRole('button', { name: '下载 ZIP' })).toBeDisabled();
  expect(screen.getByRole('textbox', { name: '步骤 1', exact: true })).toHaveValue('整理证据');
});

it('preserves each multiline validation rule when editing another field', async () => {
  draft.document.validation = ['按两项验证\n逐项记录', '不得遗漏']; await openDraft();
  fireEvent.change(screen.getByRole('textbox', { name: '步骤 1', exact: true }), { target: { value: '先查证据' } });
  fireEvent.click(screen.getByRole('button', { name: '保存草稿' })); await settle();
  expect(requests.find(row => row.method === 'PATCH').body.document.validation).toEqual(['按两项验证\n逐项记录', '不得遗漏']);
});

it('keeps generation disabled when the backend excludes it while manual entry remains', async () => {
  listed = false; generation = false; await list();
  fireEvent.click(screen.getByRole('button', { name: '新建草稿' })); await settle();
  fireEvent.click(screen.getByRole('checkbox', { name: methods[1].text }));
  expect(screen.getByRole('button', { name: '生成草稿' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '生成草稿' })); await settle();
  expect(skillCalls('/generate')).toHaveLength(0);
  expect(within(screen.getByRole('dialog')).getByRole('textbox', { name: '名称', exact: true })).toBeEnabled();
});

it('generates the selected project method through the real API without confirming it', async () => {
  listed = false; await list(); fireEvent.click(screen.getByRole('button', { name: '新建草稿' })); await settle();
  expect(screen.queryByRole('checkbox', { name: methods[0].text })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('checkbox', { name: methods[1].text }));
  fireEvent.click(screen.getByRole('button', { name: '生成草稿' })); await settle();
  expect(skillCalls('/generate')[0].body).toEqual({ sources: [{ id: methods[1].id, revision: 4 }], scene: null, key: expect.any(String) });
  expect(screen.getByRole('button', { name: '审阅' })).toBeEnabled();
  expect(screen.getByRole('button', { name: '下载 ZIP' })).toBeDisabled();
});

it('retains the same generation key for a retry with identical frozen source arguments', async () => {
  listed = false; await list(); fireEvent.click(screen.getByRole('button', { name: '新建草稿' })); await settle();
  fireEvent.click(screen.getByRole('checkbox', { name: methods[1].text }));
  delay = { ...deferred(), match: row => row.path.endsWith('/generate') };
  fireEvent.click(screen.getByRole('button', { name: '生成草稿' })); await act(async () => delay.resolve(response({}, 503)));
  delay = null; fireEvent.click(screen.getByRole('button', { name: '生成草稿' })); await settle();
  expect(skillCalls('/generate')).toHaveLength(2);
  expect(skillCalls('/generate')[1].body.key).toBe(skillCalls('/generate')[0].body.key);
});

it('replaces a conflicted generation key only after explicitly rereading an unsaved draft', async () => {
  listed = false; await list(); fireEvent.click(screen.getByRole('button', { name: '新建草稿' })); await settle();
  fireEvent.click(screen.getByRole('checkbox', { name: methods[1].text }));
  delay = { ...deferred(), match: row => row.path.endsWith('/generate') };
  fireEvent.click(screen.getByRole('button', { name: '生成草稿' }));
  await act(async () => delay.resolve(response({ detail: 'skill_generation_basis_changed' }, 409)));
  expect(screen.getByRole('alert')).toHaveTextContent('内容已变化');
  expect(screen.queryByRole('button', { name: '生成草稿' })).not.toBeInTheDocument();
  const oldKey = skillCalls('/generate')[0].body.key;
  delay = null; fireEvent.click(screen.getByRole('button', { name: '重新读取' })); await settle();
  fireEvent.click(screen.getByRole('checkbox', { name: methods[1].text }));
  fireEvent.click(screen.getByRole('button', { name: '生成草稿' })); await settle();
  expect(skillCalls('/generate')).toHaveLength(2);
  expect(skillCalls('/generate')[1].body.sources).toEqual(skillCalls('/generate')[0].body.sources);
  expect(skillCalls('/generate')[1].body.scene).toBe(skillCalls('/generate')[0].body.scene);
  expect(skillCalls('/generate')[1].body.key).not.toBe(oldKey);
  expect(screen.getByRole('button', { name: '审阅' })).toBeEnabled();
});

it('refuses regeneration when a saved method is no longer eligible instead of dropping its numbered source', async () => {
  eligible = [methods[1]]; draft.needs_update = true; await openDraft();
  fireEvent.click(screen.getByRole('button', { name: '重新生成' })); await settle();
  expect(skillCalls('/regenerate')).toHaveLength(0);
  expect(screen.getByRole('alert')).toHaveTextContent('认识已变化');
  expect(screen.getByRole('textbox', { name: '步骤 1', exact: true })).toHaveValue('整理证据');
});

it('keeps ZIP locked if the post-download revision read fails and never retries the write automatically', async () => {
  draft.reviewed = true; await openDraft(); delay = { ...deferred(), match: row => row.method === 'GET' && row.path.endsWith('/skill-one') };
  fireEvent.click(screen.getByRole('button', { name: '下载 ZIP' })); await settle();
  await act(async () => delay.resolve(response({}, 503)));
  expect(click).toHaveBeenCalledTimes(1); expect(skillCalls('/download')).toHaveLength(1);
  expect(screen.getByRole('button', { name: '下载 ZIP' })).toBeDisabled();
});

it('requires the first folder confirmation and sends the actual folder preference revision', async () => {
  folder.available = true; draft.reviewed = true; await openDraft();
  fireEvent.change(screen.getByRole('textbox', { name: '导出目录' }), { target: { value: 'D:\\temporary-skills' } });
  expect(screen.getByRole('button', { name: '导出到文件夹' })).toBeDisabled();
  fireEvent.click(screen.getByRole('checkbox', { name: '确认首次目录导出' }));
  fireEvent.click(screen.getByRole('button', { name: '导出到文件夹' })); await settle();
  expect(skillCalls('/folder')[0].body).toEqual({ expected_revision: 7, directory: 'D:\\temporary-skills', confirm_first_export: true, expected_confirmation_revision: 0 });
  expect(screen.getByText('D:\\temporary-skills/evidence-check')).toBeInTheDocument();
  expect(screen.queryByRole('checkbox', { name: '确认首次目录导出' })).not.toBeInTheDocument();
  fireEvent.change(screen.getByRole('textbox', { name: '导出目录' }), { target: { value: 'D:\\another-temporary-root' } });
  fireEvent.click(screen.getByRole('button', { name: '导出到文件夹' })); await settle();
  expect(skillCalls('/folder')[1].body).toEqual({ expected_revision: 8, directory: 'D:\\another-temporary-root', confirm_first_export: false, expected_confirmation_revision: 1 });
});

it('exposes no local directory controls without the backend desktop capability', async () => {
  await openDraft();
  expect(screen.queryByRole('textbox', { name: '导出目录' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '导出到文件夹' })).not.toBeInTheDocument();
});
it('reloads a newly saved draft by its server identity after a failed post-download read', async () => {
  listed = false; await list(); fireEvent.change(screen.getByLabelText('skill 场景'), { target: { value: '阅读' } });
  fireEvent.click(screen.getByRole('button', { name: '新建草稿' })); await settle();
  fireEvent.click(screen.getByRole('checkbox', { name: methods[0].text }));
  for (const [label, text] of [['名称', 'manual-method'], ['什么时候用', '判断前使用'], ['触发边界', '证据已收齐'], ['步骤 1', '核对证据'], ['验证规则', '每条有出处']]) {
    fireEvent.change(within(screen.getByRole('dialog')).getByRole('textbox', { name: label, exact: true }), { target: { value: text } });
  }
  fireEvent.click(screen.getByRole('button', { name: '保存草稿' })); await settle();
  fireEvent.click(screen.getByRole('button', { name: '审阅' })); await settle();
  delay = { ...deferred(), match: row => row.method === 'GET' && row.path.endsWith('/skill-one') };
  fireEvent.click(screen.getByRole('button', { name: '下载 ZIP' })); await settle(); await act(async () => delay.resolve(response({}, 503)));
  delay = null; fireEvent.click(screen.getByRole('button', { name: '重新读取' })); await settle();
  expect(within(screen.getByRole('dialog')).getByRole('textbox', { name: '名称', exact: true })).toHaveValue('manual-method');
  expect(screen.getByRole('button', { name: '下载 ZIP' })).toBeEnabled();
  expect(requests.filter(row => row.method === 'GET' && row.path.endsWith('/skill-one'))).toHaveLength(2);
  expect(requests.filter(row => row.method === 'POST' && row.path.endsWith('/skill-exports'))).toHaveLength(1);
});
