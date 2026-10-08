import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import SettingsPage from '@src/features/settings/SettingsPage';

afterEach(cleanup);
const settle = () => act(async () => {});
function fixture() {
  const value = { model: { generation_mode: { mode: 'api' },
    vision: { mode: 'local', mode_revision: 2, revision: 3, model: 'vision-fake',
      base_url: 'https://vision.invalid/v1', configured: false, allow_remote: true,
      has_api_key: true, local: { provider: 'rapidocr', status: 'unavailable' } } },
    privacy: { revision: 0, private_projects: [] } };
  const api = { load: vi.fn(async () => value), projects: vi.fn(async () => ({ items: [] })),
    saveVisionMode: vi.fn(async ({ mode }) => { value.model.vision.mode = mode; }),
    saveModel: vi.fn(async () => ({})), savePrices: vi.fn(async () => ({})) };
  return { value, api };
}

it('shows local availability and switches mode using its independent revision', async () => {
  const { api } = fixture(); render(<SettingsPage api={api}/>); await settle();
  expect(screen.queryByRole('switch', { name: '识图外发' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '识图', exact: true }));
  expect(screen.getByText('不可用')).toBeInTheDocument();
  expect(screen.queryByLabelText('识图密钥')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '外接', exact: true })); await settle();
  expect(api.saveVisionMode).toHaveBeenCalledWith({ mode: 'remote', expectedRevision: 2 });
  expect(screen.getByRole('switch', { name: '识图外发' })).toHaveAttribute('aria-checked', 'true');
  expect(screen.getByLabelText('识图密钥')).toHaveValue('');
  expect(screen.getByLabelText('识图密钥')).toHaveAttribute('placeholder', '已设置');
});

it('saves the independent vision profile and clears the key', async () => {
  const { api, value } = fixture(); value.model.vision.mode = 'remote';
  render(<SettingsPage api={api}/>); await settle();
  fireEvent.click(screen.getByRole('button', { name: '识图', exact: true }));
  fireEvent.change(screen.getByLabelText('识图密钥'), { target: { value: 'synthetic-value' } });
  fireEvent.click(screen.getByRole('button', { name: '保存', exact: true })); await settle();
  expect(api.saveModel).toHaveBeenCalledWith(expect.objectContaining({ purpose: 'vision',
    apiKey: 'synthetic-value', expectedRevision: 3, allowRemote: true }));
  expect(screen.getByLabelText('识图密钥')).toHaveValue('');
  expect(document.body).not.toHaveTextContent('synthetic-value');
  fireEvent.click(screen.getByRole('switch', { name: '识图外发' })); await settle();
  expect(api.saveModel).toHaveBeenLastCalledWith(expect.objectContaining({ purpose: 'vision',
    apiKey: '', expectedRevision: 3, allowRemote: false }));
});
