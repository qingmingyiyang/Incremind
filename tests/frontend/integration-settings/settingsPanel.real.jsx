import { afterAll, expect, it } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { copyFile, mkdir, mkdtemp } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import SettingsPage from '@src/features/settings/SettingsPage';
import { settingsApi } from '@src/features/settings/settingsApi';

// 专用配置显式运行真实集成检查，普通前端测试不启动后端。
const nativeFetch = globalThis.fetch;
let server;
afterAll(async () => {
  cleanup(); globalThis.fetch = nativeFetch;
  if (server && server.exitCode === null) { const closed = once(server, 'close'); server.kill(); await closed; }
});

it('saves only changed parts through the real ASGI service and uses current SQLite CAS between parts', async () => {
  const root = resolve(process.cwd(), '../..'), work = join(root, 'work');
  await mkdir(work, { recursive: true });
  const runtime = await mkdtemp(join(work, 'S1-live-'));
  await mkdir(join(runtime, 'config'));
  await copyFile(join(root, 'config/settings.toml.example'), join(runtime, 'config/settings.toml'));
  const origin = 'http://127.0.0.1:8001', writes = [];
  let afterModelSaved = null;
  let diagnostics = '', exited = false;
  server = spawn(join(root, '.venv/Scripts/python.exe'), ['-m', 'uvicorn', 'backend.memory_app.app:create_app', '--factory', '--host', '127.0.0.1', '--port', '8001', '--no-use-colors'], {
    cwd: root, windowsHide: true, env: { ...process.env, PYTHONPATH: join(root, 'src'), CHRIPTMAS_APP_ROOT: runtime },
    stdio: ['ignore', 'ignore', 'pipe'],
  });
  server.stderr.setEncoding('utf8');
  server.stderr.on('data', text => { diagnostics = (diagnostics + text).slice(-6000); });
  server.once('exit', () => { exited = true; });
  server.once('error', reason => { diagnostics = String(reason); exited = true; });
  const deadline = Date.now() + 120000;
  while (true) {
    if (exited) throw new Error(`isolated_settings_server_exited: ${diagnostics}`);
    let ready = false;
    try { if (diagnostics.includes('Uvicorn running on http://127.0.0.1:8001')) ready = (await nativeFetch(`${origin}/api/v2/settings`)).ok; }
    catch (reason) { if (Date.now() >= deadline) throw reason; }
    if (ready) break;
    if (Date.now() >= deadline) throw new Error(`isolated_settings_server_timeout: ${diagnostics}`);
    await new Promise(resolveWait => setTimeout(resolveWait, 250));
  }
  // 这里只将浏览器相对地址交给真实 loopback HTTP，不代替响应或业务实现。
  globalThis.fetch = async (input, options) => {
    const url = new URL(input, origin);
    if (['PUT', 'PATCH'].includes(options?.method)) writes.push({ path: url.pathname, body: JSON.parse(options.body) });
    const response = await nativeFetch(url, options);
    if (response.ok && options?.method === 'PUT' && url.pathname === '/api/v2/settings/model' && afterModelSaved) {
      const action = afterModelSaved; afterModelSaved = null; await action();
    }
    return response;
  };
  async function concurrentWrite(path, body, method = 'PATCH') {
    const response = await nativeFetch(`${origin}/api/v2/settings/${path}`, { method,
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    expect(response.ok).toBe(true);
  }
  const concurrentRead = async () => (await nativeFetch(`${origin}/api/v2/settings`)).json();
  let value = await settingsApi.load();
  await settingsApi.saveMode({ mode: 'api', localEnabled: false, localBaseUrl: value.model.generation_mode.local_base_url,
    expectedRevision: value.model.generation_mode.revision });
  value = await settingsApi.load();
  await settingsApi.saveModel({ purpose: 'generation', model: 's1-writer', baseUrl: 'https://example.invalid/v1',
    apiKey: 'test-only', enabled: false, allowRemote: false, expectedRevision: value.model.generation.revision });
  writes.length = 0;
  render(<SettingsPage/>);
  fireEvent.click(await screen.findByRole('button', { name: '生成', exact: true }));
  fireEvent.change(screen.getByLabelText('生成输入单价'), { target: { value: '2' } });
  fireEvent.click(screen.getByRole('button', { name: '保存', exact: true }));
  await screen.findByRole('button', { name: '✓ 已保存' });
  await waitFor(() => expect(screen.getByRole('button', { name: '✓ 已保存' })).not.toBeDisabled());
  expect(writes.map(row => row.path)).toEqual(['/api/v2/settings/model-prices']);
  expect((await settingsApi.load()).model.generation.pricing.rates.input_per_million).toBe('2');

  writes.length = 0;
  fireEvent.change(screen.getByLabelText('生成模型'), { target: { value: 's1-writer-two' } });
  fireEvent.change(screen.getByLabelText('快模型'), { target: { value: 's1-quick' } });
  fireEvent.change(screen.getByLabelText('生成输入单价'), { target: { value: '3' } });
  fireEvent.click(screen.getByRole('button', { name: '保存', exact: true }));
  await screen.findByRole('button', { name: '✓ 已保存' });
  await waitFor(() => expect(screen.getByRole('button', { name: '✓ 已保存' })).not.toBeDisabled());
  expect(writes.map(row => row.path)).toEqual(['/api/v2/settings/model', '/api/v2/settings/fast-model', '/api/v2/settings/model-prices']);
  value = await settingsApi.load();
  expect(value.model.generation.model).toBe('s1-writer-two');
  expect(value.model.generation.fast_model.model).toBe('s1-quick');
  expect(value.model.generation.pricing.rates.input_per_million).toBe('3');
  expect(writes[1].body.expected_generation_revision).toBe(value.model.generation.revision);
  expect(writes[1].body.expected_mode_revision).toBe(value.model.generation_mode.revision);
  expect(writes[2].body.expected_configuration_revision).toBe(value.model.generation.revision);

  const partialPriceRevision = value.model.generation.pricing.revision;
  writes.length = 0;
  fireEvent.change(screen.getByLabelText('生成模型'), { target: { value: 's1-partial-writer' } });
  fireEvent.change(screen.getByLabelText('快模型'), { target: { value: 'q'.repeat(201) } });
  fireEvent.change(screen.getByLabelText('生成输入单价'), { target: { value: '4' } });
  fireEvent.click(screen.getByRole('button', { name: '保存', exact: true }));
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('操作未完成 · 重试'));
  await waitFor(() => expect(screen.getByRole('button', { name: '保存', exact: true })).not.toBeDisabled());
  expect(writes.map(row => row.path)).toEqual(['/api/v2/settings/model', '/api/v2/settings/fast-model']);
  expect((await concurrentRead()).model.generation.model).toBe('s1-partial-writer');
  expect(screen.getByLabelText('生成输入单价')).toHaveValue(4);
  writes.length = 0;
  fireEvent.change(screen.getByLabelText('快模型'), { target: { value: 's1-recovered-quick' } });
  fireEvent.click(screen.getByRole('button', { name: '保存', exact: true }));
  await screen.findByRole('button', { name: '✓ 已保存' });
  await waitFor(() => expect(screen.getByRole('button', { name: '✓ 已保存' })).not.toBeDisabled());
  expect(writes.map(row => row.path)).toEqual(['/api/v2/settings/fast-model', '/api/v2/settings/model-prices']);
  expect(writes[1].body.expected_revision).toBe(partialPriceRevision);
  value = await concurrentRead();
  expect(value.model.generation.fast_model.model).toBe('s1-recovered-quick');
  expect(value.model.generation.pricing.rates.input_per_million).toBe('4');

  // 第二个真实客户端在模型保存与子项提交之间写入，原草稿必须遇到 CAS 冲突。
  const priceRevision = value.model.generation.pricing.revision;
  afterModelSaved = async () => {
    const latest = await concurrentRead();
    await concurrentWrite('model-prices', { purpose: 'generation', rates: { input_per_million: '9', output_per_million: null, cache_read_per_million: null },
      expected_revision: latest.model.generation.pricing.revision, expected_configuration_revision: latest.model.generation.revision });
  };
  writes.length = 0;
  fireEvent.change(screen.getByLabelText('生成模型'), { target: { value: 's1-writer-three' } });
  fireEvent.change(screen.getByLabelText('生成输入单价'), { target: { value: '5' } });
  fireEvent.click(screen.getByRole('button', { name: '保存', exact: true }));
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('设置已变化 · 刷新'));
  await waitFor(() => expect(screen.getByRole('button', { name: '保存', exact: true })).not.toBeDisabled());
  expect(writes.map(row => row.path)).toEqual(['/api/v2/settings/model', '/api/v2/settings/model-prices']);
  expect(writes[1].body.expected_revision).toBe(priceRevision);
  expect((await concurrentRead()).model.generation.pricing.rates.input_per_million).toBe('9');
  expect(screen.getByLabelText('生成输入单价')).toHaveValue(5);
  writes.length = 0;
  fireEvent.click(screen.getByRole('button', { name: '保存', exact: true }));
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('设置已变化 · 刷新'));
  await waitFor(() => expect(screen.getByRole('button', { name: '保存', exact: true })).not.toBeDisabled());
  expect(writes.map(row => row.path)).toEqual(['/api/v2/settings/model-prices']);
  expect(writes[0].body.expected_revision).toBe(priceRevision);
  expect((await concurrentRead()).model.generation.pricing.rates.input_per_million).toBe('9');

  value = await concurrentRead();
  const fastRevision = value.model.generation.fast_model.revision;
  afterModelSaved = async () => {
    const latest = await concurrentRead();
    await concurrentWrite('fast-model', { model: 's1-concurrent-quick', expected_revision: latest.model.generation.fast_model.revision,
      expected_generation_revision: latest.model.generation.revision, expected_mode_revision: latest.model.generation_mode.revision });
  };
  writes.length = 0;
  fireEvent.change(screen.getByLabelText('生成模型'), { target: { value: 's1-writer-four' } });
  fireEvent.change(screen.getByLabelText('快模型'), { target: { value: 's1-user-quick' } });
  fireEvent.change(screen.getByLabelText('生成输入单价'), { target: { value: '6' } });
  fireEvent.click(screen.getByRole('button', { name: '保存', exact: true }));
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('设置已变化 · 刷新'));
  await waitFor(() => expect(screen.getByRole('button', { name: '保存', exact: true })).not.toBeDisabled());
  expect(writes.map(row => row.path)).toEqual(['/api/v2/settings/model', '/api/v2/settings/fast-model']);
  expect(writes[1].body.expected_revision).toBe(fastRevision);
  expect((await concurrentRead()).model.generation.fast_model.model).toBe('s1-concurrent-quick');
  writes.length = 0;
  fireEvent.click(screen.getByRole('button', { name: '保存', exact: true }));
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('设置已变化 · 刷新'));
  await waitFor(() => expect(screen.getByRole('button', { name: '保存', exact: true })).not.toBeDisabled());
  expect(writes.map(row => row.path)).toEqual(['/api/v2/settings/fast-model']);
  expect(writes[0].body.expected_revision).toBe(fastRevision);
  expect((await concurrentRead()).model.generation.fast_model.model).toBe('s1-concurrent-quick');

  afterModelSaved = async () => {
    const latest = await concurrentRead();
    await concurrentWrite('model', { purpose: 'generation', model: 's1-other-model', base_url: 'https://example.invalid/v1',
      api_key: '', enabled: false, allow_remote: false, expected_revision: latest.model.generation.revision }, 'PUT');
  };
  writes.length = 0;
  fireEvent.change(screen.getByLabelText('生成模型'), { target: { value: 's1-writer-five' } });
  fireEvent.click(screen.getByRole('button', { name: '保存', exact: true }));
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('设置已变化 · 刷新'));
  await waitFor(() => expect(screen.getByRole('button', { name: '保存', exact: true })).not.toBeDisabled());
  expect(writes.map(row => row.path)).toEqual(['/api/v2/settings/model']);
  expect((await concurrentRead()).model.generation.model).toBe('s1-other-model');

  writes.length = 0;
  fireEvent.change(screen.getByLabelText('生成地址'), { target: { value: 'http://example.invalid/v1' } });
  fireEvent.change(screen.getByLabelText('生成输入单价'), { target: { value: '4' } });
  fireEvent.click(screen.getByRole('button', { name: '保存', exact: true }));
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('操作未完成 · 重试'));
  await act(async () => {});
  expect(writes.map(row => row.path)).toEqual(['/api/v2/settings/model']);
  expect(screen.getByLabelText('生成地址')).toHaveValue('http://example.invalid/v1');
  expect(screen.getByLabelText('生成输入单价')).toHaveValue(4);
  expect((await settingsApi.load()).model.generation.pricing.rates).toBeNull();
}, 180000);
