import { productFetch, downloadProductFile } from '../../shared/api/deviceTransport';
import { readResponseJson } from '../../shared/lib/responseJson';
import { libraryBackendUrl } from '../rebuild/libraryOverviewTransport';
import { ownedSharedBackups, sharedBackup, sharedBackupImport } from './sharedBackupViews';

const base = '/api/v2/server/shared-projects', part = encodeURIComponent;
async function request(path, { command, signal } = {}) {
  const response = await productFetch(libraryBackendUrl(base + path), {
    method: command ? 'POST' : 'GET', cache: 'no-store', signal,
    ...(command ? { headers: { 'Content-Type': 'application/json', 'Idempotency-Key': command.id }, body: JSON.stringify(command.body) } : {}),
  });
  const value = await readResponseJson(response);
  if (!response.ok) {
    const invalidSource = typeof value?.detail === 'string' && /(?:source_unverifiable|qualification|archive_changed|lineage|binding_changed)/.test(value.detail);
    const error = new Error(invalidSource ? '备份资料或来源未通过核验，请保留原记录并选择其他完整备份'
      : response.status === 403 ? '当前没有操作权限，请核对当前空间与项目资格'
      : response.status === 401 ? '请重新登录' : response.status === 429 ? '当前资源不足，请处理后继续原操作'
        : '备份或恢复未完成，请核对后继续');
    error.status = response.status; error.code = value?.detail; throw error;
  }
  return value;
}
export const sharedBackupApi = {
  owned: signal => request('/backups/owned', { signal }).then(ownedSharedBackups),
  read: (command, signal) => command.expected.kind === 'backup'
    ? request(`/backup-commands/${part(command.id)}`, { signal }).then(value => sharedBackup(value, { id: command.id, project: command.expected.source }))
    : request(`/backup-import-commands/${part(command.id)}`, { signal }).then(value => sharedBackupImport(value, { id: command.id, backup: command.expected.source })),
  send: (command, signal) => request(command.path, { command, signal }).then(value => command.expected.kind === 'backup'
    ? sharedBackup(value, { id: command.id, project: command.expected.source })
    : sharedBackupImport(value, { id: command.id, backup: command.expected.source })),
  download: (backup, current, signal) => downloadProductFile(libraryBackendUrl(`${base}/backups/${part(backup.command_id)}/download`),
    `chriptmas-backup-${backup.snapshot_id}.zip`, current, { signal }),
};
