import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import SettingsPage from '@src/features/settings/SettingsPage';

let value, api;
const settle = async () => act(async () => {});
beforeEach(() => {
  // 只隔离新增能力读取的后端 HTTP，页面与原失败提示仍走真实组件。
  vi.stubGlobal('fetch', vi.fn(async (url, request = {}) => {
    if (url === '/api/v2/settings/provider-store' && request.method === 'GET') {
      return { ok: true, status: 200, json: async () => ({ available: false, enabled: false,
        revision: 0, generation_revision: 3, mode_revision: 7 }) };
    }
    throw new Error('unexpected_test_backend_http');
  }));
  value = { model: {
    generation: { revision: 3, base_url: 'https://example.test/v1', model: 'writer', enabled: true, configured: true, has_api_key: true, allow_remote: true },
    generation_mode: { mode: 'api', revision: 7, local_enabled: true, local_base_url: 'http://127.0.0.1:8001/local-model/v1', local_model: 'local', local_model_installed: true },
    embedding: { revision: 4, base_url: 'https://example.test/v1', model: 'vector', enabled: true, allow_remote: false },
    rerank: { revision: 5, base_url: 'https://example.test/v1', model: 'rank', enabled: true, allow_remote: true },
    asr: { settings_revision: 9, enabled: true, model: 'hy-asr', endpoint: 'https://asr.test', max_audio_bytes: 12000000, has_api_key: true, egress_manifest: { manifest_id: 'asr-m', consented: true } },
  }, privacy: { revision: 6, private_projects: ['alpha'] } };
  api = { load: vi.fn(async () => value), projects: vi.fn(async () => ({ items: [{ id: 'alpha', name: '阅读', revision: 2, scenes: ['读书'] }, { id: 'me', name: '我', builtin: 'me', revision: 1 }, { id: 'inbox', name: '收件箱', builtin: 'inbox', revision: 1 }] })),
    saveModel: vi.fn(async () => ({})), saveMode: vi.fn(async () => ({})), testModel: vi.fn(async () => ({ status: 'complete' })),
    saveFastModel: vi.fn(async () => ({})), fastModels: vi.fn(async () => ({ items: [] })),
    disableAsr: vi.fn(async () => { value.model.asr.egress_manifest.consented = false; }), enableAsr: vi.fn(async () => ({})),
    privateSources: vi.fn(async () => [{ source_id: 'x', title: '私密材料', type: 'experience', project_id: 'alpha', policy_revision: 2, source_revision: 8, inherited: false }, { source_id: 'y', title: '继承材料', type: 'recognition', project_id: 'alpha', policy_revision: 3, inherited: true }]),
    cancelPrivate: vi.fn(async () => { api.privateSources.mockResolvedValue([{ source_id: 'y', title: '继承材料', type: 'recognition', project_id: 'alpha', inherited: true }]); }),
    receipts: vi.fn(async () => [{ at: null, purpose: '问', model: 'writer', items: 2, usage: { input: 90, output: 20 } }]),
    loadConstraints: vi.fn(async () => ({ items: [] })), saveConstraint: vi.fn(async () => ({})),
    savePrivacy: vi.fn(async () => ({})), saveProject: vi.fn(async () => ({})), createProject: vi.fn(async () => ({})),
  };
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
it('orders model labels, values, status and egress and reports expansion', async () => {
  render(<SettingsPage api={api}/>); await settle();
  const trigger = screen.getByRole('button', { name: '生成', exact: true });
  const row = trigger.closest('.ui-row');
  expect(trigger).toHaveAttribute('aria-expanded', 'false');
  const valueLabel = within(row).getByText('writer');
  const status = within(row).getByRole('img', { name: '已沉淀' });
  const toggle = within(row).getByRole('switch', { name: '生成外发' });
  expect(trigger.compareDocumentPosition(valueLabel) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(valueLabel.compareDocumentPosition(status) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(status.compareDocumentPosition(toggle) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  fireEvent.click(trigger);
  expect(trigger).toHaveAttribute('aria-expanded', 'true');
  fireEvent.click(trigger);
  expect(trigger).toHaveAttribute('aria-expanded', 'false');
  fireEvent.click(screen.getByRole('button', { name: '隐私', exact: true }));
  const privateTrigger = screen.getByRole('button', { name: '私密资料' });
  expect(privateTrigger).toHaveAttribute('aria-expanded', 'false');
  fireEvent.click(privateTrigger); await settle();
  expect(privateTrigger).toHaveAttribute('aria-expanded', 'true');
  fireEvent.click(screen.getByRole('button', { name: '项目', exact: true }));
  const projectTrigger = screen.getByRole('button', { name: '阅读', exact: true });
  expect(projectTrigger).toHaveAttribute('aria-expanded', 'false');
  fireEvent.click(projectTrigger);
  expect(projectTrigger).toHaveAttribute('aria-expanded', 'true');
});
it('keeps every model key input empty and submits each purpose revision', async () => {
  render(<SettingsPage api={api}/>); await settle();
  fireEvent.click(screen.getByRole('button', { name: '生成' }));
  expect(screen.getByLabelText('生成密钥')).toHaveValue(''); expect(screen.getByLabelText('生成密钥')).toHaveAttribute('placeholder', '已设置');
  fireEvent.click(screen.getByRole('switch', { name: '生成外发' })); await settle();
  expect(api.saveModel).toHaveBeenCalledWith(expect.objectContaining({ purpose: 'generation', expectedRevision: 3, allowRemote: false, apiKey: '' }));
  fireEvent.click(screen.getByRole('switch', { name: '向量外发' })); await settle();
  expect(api.saveModel).toHaveBeenLastCalledWith(expect.objectContaining({ purpose: 'embedding', expectedRevision: 4, allowRemote: true }));
  fireEvent.click(screen.getByRole('switch', { name: '重排外发' })); await settle();
  expect(api.saveModel).toHaveBeenLastCalledWith(expect.objectContaining({ purpose: 'rerank', expectedRevision: 5, allowRemote: false }));
  expect(screen.getByLabelText('生成密钥')).toHaveValue('');
});
it('revokes transcription consent when switching it off', async () => {
  render(<SettingsPage api={api}/>); await settle();
  fireEvent.click(screen.getByRole('switch', { name: '转写外发' })); await settle();
  expect(api.disableAsr).toHaveBeenCalledOnce(); expect(screen.getByRole('switch', { name: '转写外发' })).toHaveAttribute('aria-checked', 'false');
});
it('disables egress in local mode and displays the installed model', async () => {
  value.model.generation_mode.mode = 'local';
  render(<SettingsPage api={api}/>); await settle();
  expect(screen.getByRole('switch', { name: '生成外发' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '生成' }));
  expect(screen.getByText('已安装')).toBeInTheDocument(); expect(screen.queryByLabelText('生成密钥')).not.toBeInTheDocument();
});
it('cancels only direct private sources and refreshes the list', async () => {
  render(<SettingsPage api={api} initialSection="privacy"/>); await settle();
  fireEvent.click(screen.getByRole('button', { name: '私密资料' })); await settle();
  const direct = screen.getByText('私密材料').closest('.ui-row'); const inherited = screen.getByText('继承材料').closest('.ui-row');
  expect(within(inherited).queryByRole('button', { name: '取消私密' })).not.toBeInTheDocument(); expect(inherited).toHaveTextContent('阅读');
  fireEvent.click(within(direct).getByRole('button', { name: '取消私密' })); await settle();
  expect(api.cancelPrivate).toHaveBeenCalledWith(expect.objectContaining({ source_id: 'x', policy_revision: 2 }));
  expect(screen.queryByText('私密材料')).not.toBeInTheDocument();
});
it('renders missing egress metadata as unknown and preserves real usage', async () => {
  render(<SettingsPage api={api} initialSection="privacy"/>); await settle();
  fireEvent.click(screen.getByRole('button', { name: '外发记录' })); await settle();
  expect(screen.getByRole('table')).toHaveTextContent('—'); expect(screen.getByRole('table')).toHaveTextContent('90 / 20');
});
it('opens the requested project and hides inbox constraints and persona fields', async () => {
  render(<SettingsPage api={api} initialSection="project" expandProject="inbox"/>); await settle();
  const inbox = screen.getByRole('region', { name: '收件箱设置' }); expect(within(inbox).queryByLabelText('新约束')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '我' })); await settle();
  const persona = screen.getByRole('region', { name: '我设置' }); expect(within(persona).queryByLabelText('名称')).not.toBeInTheDocument(); expect(within(persona).queryByRole('switch', { name: '私密' })).not.toBeInTheDocument();
});

it('clears a typed key after save and keeps failure visible without exposing it', async () => {
  api.saveModel.mockRejectedValue(new Error('model_update_failed'));
  render(<SettingsPage api={api}/>); await settle(); fireEvent.click(screen.getByRole('button', { name: '生成' }));
  fireEvent.change(screen.getByLabelText('生成密钥'), { target: { value: 'sk-test-DO-NOT-LEAK' } });
  fireEvent.click(screen.getByRole('button', { name: '保存' })); await settle();
  expect(api.saveModel).toHaveBeenCalledWith(expect.objectContaining({ apiKey: 'sk-test-DO-NOT-LEAK', expectedRevision: 3 }));
  expect(screen.getByLabelText('生成密钥')).toHaveValue(''); expect(screen.getByRole('alert')).toHaveTextContent('操作未完成 · 重试'); expect(screen.getByRole('alert')).not.toHaveTextContent('model_update_failed'); expect(document.body).not.toHaveTextContent('sk-test-DO-NOT-LEAK');
});
it('writes the privacy revision and the project revision to separate endpoints', async () => {
  render(<SettingsPage api={api} initialSection="privacy"/>); await settle();
  fireEvent.click(screen.getByRole('button', { name: '移除阅读私密' })); await settle(); expect(api.savePrivacy).toHaveBeenCalledWith([], 6);
  fireEvent.click(screen.getByRole('button', { name: '项目', exact: true })); fireEvent.click(screen.getByRole('button', { name: '阅读' }));
  fireEvent.click(screen.getByRole('switch', { name: '私密' })); await settle(); expect(api.saveProject).toHaveBeenCalledWith(expect.objectContaining({ id: 'alpha', revision: 2 }), { private: true });
});

it('waits for the API profile before mounting editable generation fields', async () => {
  value.model.generation_mode.mode = 'subscription';
  value.model.generation.base_url = 'https://subscription.test/responses';
  value.model.generation.model = 'subscription-writer';
  let finish;
  api.saveMode.mockImplementation(() => new Promise(resolve => { finish = () => {
    value = { ...value, model: { ...value.model, generation_mode: { ...value.model.generation_mode, mode: 'api' }, generation: { ...value.model.generation, base_url: 'https://original-api.test/v1', model: 'original-writer' } } };
    resolve({});
  }; }));
  render(<SettingsPage api={api}/>); await settle();
  fireEvent.click(screen.getByRole('button', { name: '生成' }));
  fireEvent.click(screen.getByRole('button', { name: 'API', exact: true }));
  expect(screen.queryByLabelText('生成地址')).not.toBeInTheDocument();
  await act(async () => finish()); await settle();
  expect(screen.getByLabelText('生成地址')).toHaveValue('https://original-api.test/v1');
  expect(screen.getByLabelText('生成模型')).toHaveValue('original-writer');
  fireEvent.click(screen.getByRole('button', { name: '保存' })); await settle();
  expect(api.saveModel).toHaveBeenCalledWith(expect.objectContaining({ baseUrl: 'https://original-api.test/v1', model: 'original-writer', expectedRevision: 3 }));
});

it('enables a newly configured retrieval purpose when authorizing its use', async () => {
  value.model.embedding.enabled = false;
  render(<SettingsPage api={api}/>); await settle();
  fireEvent.click(screen.getByRole('switch', { name: '向量外发' })); await settle();
  expect(api.saveModel).toHaveBeenCalledWith(expect.objectContaining({ purpose: 'embedding', enabled: true, allowRemote: true, expectedRevision: 4 }));
});

it('shows fixed ASR values without edit controls or a test action', async () => {
  render(<SettingsPage api={api}/>); await settle(); fireEvent.click(screen.getByRole('button', { name: '转写' }));
  expect(screen.queryByRole('textbox', { name: '转写地址' })).not.toBeInTheDocument();
  expect(screen.queryByRole('textbox', { name: '转写模型' })).not.toBeInTheDocument();
  expect(screen.getByText('https://asr.test')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '测试' })).not.toBeInTheDocument();
  expect(screen.getByLabelText('转写密钥')).toHaveValue('');
});
it('hides cancel when either private source revision is unavailable', async () => {
  api.privateSources.mockResolvedValue([{ source_id: 'x', title: '缺修订', type: 'experience', project_id: 'alpha', policy_revision: 2, inherited: false }]);
  render(<SettingsPage api={api} initialSection="privacy"/>); await settle();
  fireEvent.click(screen.getByRole('button', { name: '私密资料' })); await settle();
  expect(screen.getByText('缺修订')).toBeInTheDocument(); expect(screen.queryByRole('button', { name: '取消私密' })).not.toBeInTheDocument();
});
