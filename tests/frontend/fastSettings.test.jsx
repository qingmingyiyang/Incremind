import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import SettingsPage from '@src/features/settings/SettingsPage';
import { settingsApi } from '@src/features/settings/settingsApi';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const settle = async () => act(async () => {});
function api(mode = 'api') {
  return { load: vi.fn(async () => ({ model: {
    generation: { model: 'main', revision: 3, configured: true, fast_model: { model: 'quick', revision: 4, configured: true } },
    generation_mode: { mode, revision: 7, local_model: 'qwen2.5-1.5b-instruct', local_model_installed: true },
  }, privacy: { private_projects: [] } })), projects: vi.fn(async () => ({ items: [] })),
    saveFastModel: vi.fn(async () => ({})), fastModels: vi.fn(async () => ({ items: [{ id: 'quick', name: 'Quick' }] })) };
}

it('saves and clears fast choice with independent and parent revisions', async () => {
  const service = api(); render(<SettingsPage api={service}/>); await settle();
  fireEvent.click(screen.getByRole('button', { name: '生成' }));
  expect(screen.getByLabelText('快模型')).toHaveValue('quick');
  fireEvent.change(screen.getByLabelText('快模型'), { target: { value: '' } });
  fireEvent.click(screen.getByRole('button', { name: '保存快模型' })); await settle();
  expect(service.saveFastModel).toHaveBeenCalledExactlyOnceWith({ model: null, expectedRevision: 4,
    expectedGenerationRevision: 3, expectedModeRevision: 7 });
});

it('uses the subscription catalog while local keeps a fixed model', async () => {
  const service = api('subscription'); render(<SettingsPage api={service}/>); await settle();
  fireEvent.click(screen.getByRole('button', { name: '生成' })); await settle();
  expect(service.fastModels).toHaveBeenCalledOnce();
  expect(screen.getByRole('combobox', { name: '快模型' })).toHaveValue('quick');
  cleanup(); render(<SettingsPage api={api('local')}/>); await settle();
  fireEvent.click(screen.getByRole('button', { name: '生成' }));
  expect(screen.queryByRole('textbox', { name: '快模型' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '保存快模型' })).not.toBeInTheDocument();
});

it('sends safe fast-model PATCH through the actual settings transport', async () => {
  const fetch = vi.fn(async () => ({ ok: true, json: async () => ({}) })); vi.stubGlobal('fetch', fetch);
  await settingsApi.saveFastModel({ model: 'quick', expectedRevision: 4, expectedGenerationRevision: 3, expectedModeRevision: 7 });
  expect(fetch).toHaveBeenCalledOnce();
  const [url, request] = fetch.mock.calls[0]; expect(url).toBe('/api/v2/settings/fast-model');
  expect(request.method).toBe('PATCH');
  expect(JSON.parse(request.body)).toEqual({ model: 'quick', expected_revision: 4,
    expected_generation_revision: 3, expected_mode_revision: 7 });
});
