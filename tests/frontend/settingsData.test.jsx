import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { SettingsData } from '@src/features/settings/SettingsData';
import { createMemorySnapshot } from '@src/features/rebuild/localMemoryVaultApi';
afterEach(cleanup);
it('passes only safe snapshot failure codes through the API adapter', async () => {
  for (const [detail, code] of [['backup_source_changed', 'backup_source_changed'], ['private path /secret', 'backup_failed']]) {
    await expect(createMemorySnapshot({ fetchImpl: async () => ({ ok: false, status: 400, json: async () => ({ detail }) }) })).rejects.toMatchObject({ code, message: code });
  }
  await expect(createMemorySnapshot({ fetchImpl: async () => ({ ok: false, status: 503 }) })).rejects.toMatchObject({ code: 'server_unavailable' });
  await expect(createMemorySnapshot({ fetchImpl: async () => ({ ok: false, status: 400, json: async () => { throw new Error('private HTML'); } }) })).rejects.toMatchObject({ code: 'invalid_response' });
});
it('shows the safe backup failure code in the backup row', async () => {
  const failure = Object.assign(new Error('private file path must stay hidden'), { code: 'backup_source_changed' });
  render(<SettingsData projectId="alpha" loadSnapshots={async () => ({ snapshots: [] })} createSnapshot={async () => { throw failure; }}/>);
  fireEvent.click(screen.getByRole('button', { name: '备份' })); await act(async () => {});
  fireEvent.click(screen.getByRole('button', { name: '立即备份' })); await act(async () => {});
  expect(screen.getByRole('alert')).toHaveTextContent('✕ backup_source_changed');
  expect(screen.getByRole('alert').closest('.ui-row')).not.toBeNull();
  expect(screen.queryByText(/private file path/)).not.toBeInTheDocument();
});
it('exposes the backup disclosure state', async () => {
  render(<SettingsData projectId="alpha" loadSnapshots={async () => ({snapshots: []})}/>);
  const trigger = screen.getByRole('button', { name: '备份' });
  expect(trigger).toHaveAttribute('aria-expanded', 'false');
  fireEvent.click(trigger); await act(async () => {});
  expect(trigger).toHaveAttribute('aria-expanded', 'true');
  fireEvent.click(trigger);
  expect(trigger).toHaveAttribute('aria-expanded', 'false');
});
it('reads real snapshot dates and rereads after creating a backup, keeping size unknown', async () => {
  const load = vi.fn().mockResolvedValueOnce({ snapshots: [{ snapshot_id: 'a', created_at: '2026-10-01' }] }).mockResolvedValue({ snapshots: [{ snapshot_id: 'b', created_at: '2026-10-02' }] });
  const create = vi.fn().mockResolvedValue({});
  render(<SettingsData projectId="alpha" loadSnapshots={load} createSnapshot={create}/>);
  fireEvent.click(screen.getByRole('button', { name: '备份' })); await act(async () => {});
  expect(screen.getByText('2026-10-01')).toBeInTheDocument(); expect(screen.getByText('—')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '立即备份' })); await act(async () => {});
  expect(create).toHaveBeenCalledOnce(); expect(load).toHaveBeenCalledTimes(2); expect(screen.getByText('2026-10-02')).toBeInTheDocument();
});

it('shows measured backup bytes and only the backup action', async () => {
  render(<SettingsData projectId="alpha" loadSnapshots={async () => ({ snapshots: [{snapshot_id: 'a', created_at: 'today', size_bytes: 1234}] })}/>);
  fireEvent.click(screen.getByRole('button', { name: '备份' })); await act(async () => {});
  expect(screen.getByText('1234 B')).toBeInTheDocument();
  for (const label of ['导入', '导出', '迁移旧记忆']) expect(screen.queryByText(label)).not.toBeInTheDocument();
});

it('shows verification beside each date with a safe reason only in the title', async () => {
  render(<SettingsData projectId="alpha" loadSnapshots={async () => ({ snapshots: [
    { snapshot_id: 'good', created_at: 'good-day', verified: true, size_bytes: 12 },
    { snapshot_id: 'bad', created_at: 'bad-day', verified: false, verification_reason: 'backup_verification_failed' },
  ] })}/>);
  fireEvent.click(screen.getByRole('button', { name: '备份' })); await act(async () => {});
  expect(screen.getByText('good-day').closest('.ui-row')).toHaveTextContent('✓');
  const bad = screen.getByText('bad-day').closest('.ui-row');
  expect(bad).toHaveTextContent('✕');
  expect(bad.querySelector('[title="backup_verification_failed"]')).not.toBeNull();
  expect(screen.queryByText('backup_verification_failed')).not.toBeInTheDocument();
});
