import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import SettingsPage from '@src/features/settings/SettingsPage';

let value, api;
const settle = async () => act(async () => {});
const vectorRow = () => screen.getByRole('button', { name: '向量', exact: true }).closest('.settings-item');
beforeEach(() => {
  value = { model: { generation: {}, generation_mode: { mode: 'api', revision: 0 },
    embedding: { mode: 'local', mode_revision: 3, revision: 3,
      local: { status: 'missing', dims: 256, can_install: true } }, rerank: {}, asr: {} },
    privacy: { revision: 0, private_projects: [] } };
  api = { load: vi.fn(async () => value), projects: vi.fn(async () => ({ items: [] })),
    saveEmbeddingMode: vi.fn(async () => ({})), installEmbedding: vi.fn(async () => ({})),
    saveModel: vi.fn(async () => ({})), testModel: vi.fn(async () => ({ status: 'complete' })) };
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

it('shows actual download size and installs with frozen mode revision', async () => {
  render(<SettingsPage api={api}/>); await settle();
  const row = within(vectorRow());
  expect(row.getByText('≈ 1.53 GB')).toHaveAttribute('title', '安装后约 579 MB，文字权重约 542 MB');
  expect(row.queryByRole('switch', { name: '向量外发' })).toBeNull();
  fireEvent.click(row.getByRole('button', { name: '安装', exact: true })); await settle();
  expect(api.installEmbedding).toHaveBeenCalledWith(3);
});

it('chooses remote mode through independent revision without rewriting credentials', async () => {
  render(<SettingsPage api={api}/>); await settle();
  fireEvent.click(within(vectorRow()).getByRole('button', { name: '外接', exact: true })); await settle();
  expect(api.saveEmbeddingMode).toHaveBeenCalledWith({ mode: 'remote', expectedRevision: 3 });
  expect(api.saveModel).not.toHaveBeenCalled();
});

it('preserves remote model fields and outbound switch after choosing remote', async () => {
  value.model.embedding = { mode: 'remote', mode_revision: 4, revision: 9,
    model: 'original-vector', base_url: 'https://example.test/v1', allow_remote: true, enabled: true };
  render(<SettingsPage api={api}/>); await settle();
  const row = within(vectorRow());
  expect(row.getByRole('switch', { name: '向量外发' })).toHaveAttribute('aria-checked', 'true');
  fireEvent.click(row.getByRole('button', { name: '向量', exact: true }));
  expect(row.getByRole('textbox', { name: '向量模型' })).toHaveValue('original-vector');
  expect(row.getByRole('textbox', { name: '向量地址' })).toHaveValue('https://example.test/v1');
});

it('shows ready model and dimension on the original row', async () => {
  value.model.embedding.local.status = 'ready';
  render(<SettingsPage api={api}/>); await settle();
  const row = within(vectorRow());
  expect(row.getByText('EmbeddingGemma 2 · 256')).toBeTruthy();
  expect(row.queryByRole('button', { name: '安装' })).toBeNull();
});

it('shows exact download progress with accessible counts', async () => {
  value.model.embedding.local = { status: 'installing', progress: { done: 25, total: 100 } };
  render(<SettingsPage api={api}/>); await settle();
  const progress = within(vectorRow()).getByRole('progressbar', { name: '下载' });
  expect(progress).toHaveAttribute('aria-valuenow', '25');
  expect(progress).toHaveAttribute('aria-valuemax', '100');
  expect(within(vectorRow()).getByText('25%')).toBeTruthy();
});

it('shows indexed count while preserving unfinished work', async () => {
  value.model.embedding.local = { status: 'ready', dims: 256, index: { done: 3, total: 7 } };
  render(<SettingsPage api={api}/>); await settle();
  expect(within(vectorRow()).getByRole('progressbar', { name: '已索引' })).toHaveAttribute('aria-valuenow', '3');
  expect(within(vectorRow()).getByText('3 / 7')).toBeTruthy();
});

it('keeps failure code in title and retries through the original install operation', async () => {
  value.model.embedding.local = { status: 'failed', can_install: true, reason_code: 'embedding_install_interrupted' };
  render(<SettingsPage api={api}/>); await settle();
  const row = within(vectorRow());
  expect(row.getByLabelText('向量安装失败')).toHaveAttribute('title', 'embedding_install_interrupted');
  fireEvent.click(row.getByRole('button', { name: '重试', exact: true })); await settle();
  expect(api.installEmbedding).toHaveBeenCalledWith(3);
});

it('does not offer install to ordinary server users', async () => {
  value.model.embedding.local.can_install = false;
  render(<SettingsPage api={api}/>); await settle();
  expect(within(vectorRow()).queryByRole('button', { name: '安装' })).toBeNull();
  expect(within(vectorRow()).getByText('≈ 1.53 GB')).toBeTruthy();
});

it('polls pending progress and stops on unmount', async () => {
  vi.useFakeTimers();
  value.model.embedding.local = { status: 'installing', progress: { done: 0, total: 100 } };
  const view = render(<SettingsPage api={api}/>); await settle();
  expect(api.load).toHaveBeenCalledTimes(1);
  await act(async () => { vi.advanceTimersByTime(1000); });
  expect(api.load).toHaveBeenCalledTimes(2);
  view.unmount();
  await act(async () => { vi.advanceTimersByTime(5000); });
  expect(api.load).toHaveBeenCalledTimes(2);
});
it('does not poll an old unfinished local index while remote mode is selected', async () => {
  vi.useFakeTimers();
  value.model.embedding.mode = 'remote';
  value.model.embedding.local = { status: 'ready', dims: 256, index: { done: 3, total: 7 }, index_reason_code: null };
  render(<SettingsPage api={api}/>); await settle();
  expect(api.load).toHaveBeenCalledTimes(1);
  await act(async () => { vi.advanceTimersByTime(3000); });
  expect(api.load).toHaveBeenCalledTimes(1);
});

it('does not poll an old unfinished index when local dependencies or weights are missing', async () => {
  vi.useFakeTimers();
  value.model.embedding.local = { status: 'missing', dims: 256, index: { done: 3, total: 7 },
    index_reason_code: null, reason_code: 'local_vector_dependencies_missing' };
  render(<SettingsPage api={api}/>); await settle();
  expect(api.load).toHaveBeenCalledTimes(1);
  await act(async () => { vi.advanceTimersByTime(3000); });
  expect(api.load).toHaveBeenCalledTimes(1);
});

it('continues polling an unfinished index when local vectors are ready', async () => {
  vi.useFakeTimers();
  value.model.embedding.local = { status: 'ready', dims: 256, index: { done: 3, total: 7 }, index_reason_code: null };
  const view = render(<SettingsPage api={api}/>); await settle();
  expect(api.load).toHaveBeenCalledTimes(1);
  await act(async () => { vi.advanceTimersByTime(1000); });
  expect(api.load).toHaveBeenCalledTimes(2);
  await act(async () => { vi.advanceTimersByTime(1000); });
  expect(api.load).toHaveBeenCalledTimes(3);
  view.unmount();
  await act(async () => { vi.advanceTimersByTime(3000); });
  expect(api.load).toHaveBeenCalledTimes(3);
});
