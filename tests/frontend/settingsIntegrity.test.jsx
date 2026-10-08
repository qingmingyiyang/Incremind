import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { SettingsData } from '@src/features/settings/SettingsData';
import { settingsApi } from '@src/features/settings/settingsApi';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
it('checks through the read-only v2 settings endpoint', async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ ok: true, problems: [] }) });
  vi.stubGlobal('fetch', fetch);
  await settingsApi.integrity();
  expect(fetch).toHaveBeenCalledWith('/api/v2/settings/integrity', expect.objectContaining({ method: 'GET', cache: 'no-store' }));
  expect(fetch.mock.calls[0][1].body).toBeUndefined();
});
it('places the check under backup and shows a clean check mark on demand', async () => {
  const check = vi.fn().mockResolvedValue({ ok: true, problems: [] });
  render(<SettingsData projectId="alpha" checkIntegrity={check}/>);
  expect(check).not.toHaveBeenCalled();
  expect(screen.getAllByRole('button').map(button => button.textContent)).toEqual(['备份', '检查']);
  fireEvent.click(screen.getByRole('button', { name: '检查' }));
  expect(await screen.findByText('✓')).toBeTruthy();
  expect(check).toHaveBeenCalledOnce();
});
it('shows the total and expands only the reason codes and counts', async () => {
  const check = vi.fn().mockResolvedValue({ ok: false, problems: [
    { code: 'sqlite_corrupt', count: 1 }, { code: 'source_file_missing', count: 2 },
    { code: 'source_without_document', count: 3 }] });
  render(<SettingsData projectId="alpha" checkIntegrity={check}/>);
  fireEvent.click(screen.getByRole('button', { name: '检查' }));
  expect(await screen.findByText('✕ 6')).toBeTruthy();
  expect(screen.queryByText('sqlite_corrupt')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '检查' }));
  for (const code of ['sqlite_corrupt', 'source_file_missing', 'source_without_document']) {
    expect(screen.getByText(code).closest('.ui-row')).toHaveClass('settings-integrity-reason');
  }
  expect(screen.getByRole('button', { name: '检查' })).toHaveAttribute('aria-expanded', 'true');
});
it('keeps private errors hidden and permits another check', async () => {
  const check = vi.fn().mockRejectedValueOnce(new Error('private/path')).mockResolvedValueOnce({ ok: true, problems: [] });
  render(<SettingsData projectId="alpha" checkIntegrity={check}/>);
  fireEvent.click(screen.getByRole('button', { name: '检查' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('重试');
  expect(screen.queryByText(/private/)).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '检查' }));
  expect(await screen.findByText('✓')).toBeTruthy();
});
it('ignores a response from the previous project scope', async () => {
  let resolve;
  const check = () => new Promise(done => { resolve = done; });
  const view = render(<SettingsData projectId="alpha" checkIntegrity={check}/>);
  fireEvent.click(screen.getByRole('button', { name: '检查' }));
  view.rerender(<SettingsData projectId="beta" checkIntegrity={check}/>);
  await act(async () => { resolve({ ok: true, problems: [] }); });
  expect(screen.queryByText('✓')).toBeNull();
});
