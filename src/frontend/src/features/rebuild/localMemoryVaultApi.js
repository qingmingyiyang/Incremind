import { productFetch as fetch } from '../../shared/api/deviceTransport';
// 阶段 6：本地记忆资产库 API。
// 后端路由：
//   GET  /api/rebuild/vault-status
//   GET  /api/rebuild/provider-boundary
//   POST /api/rebuild/memory-assets/export
//   GET  /api/rebuild/memory-snapshots
//   POST /api/rebuild/memory-snapshots
//   POST /api/rebuild/memory-snapshots/{id}/rollback-plan
//   POST /api/rebuild/memory-snapshots/{id}/rollback
import { readResponseJson, responseFailure } from '../../shared/lib/responseJson';
import { deviceIdentity, downloadResponseBlob, isDeviceIdentity } from '../../shared/api/deviceTransport';

export const VAULT_STATUS_ENDPOINT = "/api/rebuild/vault-status";
export const STORAGE_GOVERNANCE_ENDPOINT = "/api/rebuild/retention/storage-governance";
export const PROVIDER_BOUNDARY_ENDPOINT = "/api/rebuild/provider-boundary";
export const MEMORY_ASSETS_EXPORT_ENDPOINT = "/api/rebuild/memory-assets/export";
export const MEMORY_SNAPSHOTS_ENDPOINT = "/api/rebuild/memory-snapshots";
export const BACKUPS_ENDPOINT = '/api/v2/backups';

function workerUrl(endpoint) {
  const electronBackend = globalThis.electronAPI?.backendBaseUrl;
  if (electronBackend) {
    return `${electronBackend.replace(/\/$/, "")}${endpoint}`;
  }

  return endpoint;
}

export async function loadVaultStatus({ fetchImpl = fetch } = {}) {
  const response = await fetchImpl(workerUrl(VAULT_STATUS_ENDPOINT), {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`Vault 状态暂时不可用 (${response.status})`);
  }
  return response.json();
}

export async function loadStorageGovernance({ fetchImpl = fetch } = {}) {
  const response = await fetchImpl(workerUrl(STORAGE_GOVERNANCE_ENDPOINT), {
    headers: { Accept: "application/json" },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(
      payload.detail || `存储治理状态暂时不可用 (${response.status})`,
    );
  }
  return payload;
}

export async function loadProviderBoundary({ fetchImpl = fetch } = {}) {
  const response = await fetchImpl(workerUrl(PROVIDER_BOUNDARY_ENDPOINT), {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`Provider 边界暂时不可用 (${response.status})`);
  }
  return response.json();
}

export async function exportMemoryAssetPackage({ fetchImpl = fetch } = {}) {
  const response = await fetchImpl(
    workerUrl(MEMORY_ASSETS_EXPORT_ENDPOINT),
    {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({}),
    },
  );
  if (!response.ok) {
    throw new Error(`导出失败，请稍后重试 (${response.status})`);
  }
  return response.json();
}

export async function listMemorySnapshots({ fetchImpl = fetch } = {}) {
  const response = await fetchImpl(workerUrl(MEMORY_SNAPSHOTS_ENDPOINT), {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`快照列表暂时不可用 (${response.status})`);
  }
  return response.json();
}

export async function createMemorySnapshot({ label, notes, fetchImpl = fetch } = {}) {
  const response = await fetchImpl(workerUrl(MEMORY_SNAPSHOTS_ENDPOINT), {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ label: label || "", notes: notes || "" }),
  });
  const payload = await readResponseJson(response);
  if (!response.ok) {
    const code = ['backup_source_changed', 'backup_sqlite_busy', 'backup_sqlite_failed', 'backup_failed'].includes(payload.detail) ? payload.detail : 'backup_failed';
    throw responseFailure(code, code, response.status);
  }
  return payload;
}

export async function createRollbackPlan({ snapshotId, fetchImpl = fetch } = {}) {
  if (!snapshotId) {
    throw new Error("snapshotId is required");
  }
  const endpoint = workerUrl(
    `${MEMORY_SNAPSHOTS_ENDPOINT}/${encodeURIComponent(snapshotId)}/rollback-plan`,
  );
  const response = await fetchImpl(endpoint, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  const payload = await readResponseJson(response);
  if (!response.ok) {
    throw responseFailure(payload.detail || 'restore_preview_failed', '恢复预览未完成', response.status);
  }
  return payload;
}

export async function releaseRestorePreview({ previewId, identity, keepalive = false } = {}) {
  if (!previewId || identity && !isDeviceIdentity(identity)) return { status: 'identity_changed' };
  const response = await fetch(workerUrl(`${BACKUPS_ENDPOINT}/restore-previews/${encodeURIComponent(previewId)}/release`), {
    method: 'POST', headers: { Accept: 'application/json' }, keepalive,
  });
  const payload = await readResponseJson(response);
  if (!response.ok) throw responseFailure(payload.detail || 'restore_preview_release_failed', '恢复预览暂未取消', response.status);
  return payload;
}

export async function confirmRestoreSafetyGate({
  snapshotId,
  rollbackId,
  confirm = true,
  execute = false,
  operationId,
  fetchImpl = fetch,
} = {}) {
  if (!snapshotId) {
    throw new Error("snapshotId is required");
  }
  if (!rollbackId) {
    throw new Error("rollbackId is required");
  }
  const endpoint = workerUrl(
    `${MEMORY_SNAPSHOTS_ENDPOINT}/${encodeURIComponent(snapshotId)}/rollback`,
  );
  const response = await fetchImpl(endpoint, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ rollback_id: rollbackId, confirm, ...(execute ? { execute: true } : {}), ...(operationId ? { operation_id: operationId } : {}) }),
  });
  const payload = await readResponseJson(response);
  if (!response.ok) {
    throw responseFailure(payload.detail || 'restore_confirmation_failed', '恢复安全门确认未完成', response.status);
  }
  if (payload.status === 'rejected') throw responseFailure('restore_plan_changed', '当前资料已变化，需要重新核对', response.status);
  return payload;
}

export async function restoreMemorySnapshot({ snapshotId, rollbackId, operationId, useDesktop = true } = {}) {
  if (!snapshotId || !rollbackId) throw new Error('restore_identity_required');
  const desktop = globalThis.electronAPI;
  if (useDesktop && typeof desktop?.restoreMemorySnapshot === 'function') {
    const result = await desktop.restoreMemorySnapshot(snapshotId, rollbackId);
    if (!['cancelled', 'restored_relaunching', 'recovery_pending_relaunch'].includes(result?.status))
      throw new Error('invalid_restore_result');
    return { ...result, target_snapshot_id: snapshotId, restore_executed: result.status === 'restored_relaunching' };
  }
  return confirmRestoreSafetyGate({ snapshotId, rollbackId, operationId, execute: true });
}

export async function loadMemoryRestore({ operationId } = {}) {
  if (!operationId) throw new Error('restore_operation_required');
  const response = await fetch(workerUrl(`/api/rebuild/memory-restores/${encodeURIComponent(operationId)}`), {
    headers: { Accept: 'application/json' },
  });
  const payload = await readResponseJson(response);
  if (!response.ok) {
    const code = response.status === 404 && payload.detail === 'restore_operation_not_found' ? 'restore_operation_not_found' : 'restore_status_unavailable';
    throw responseFailure(code, code, response.status);
  }
  return payload;
}

export async function resumeMemoryRestore({ operationId } = {}) {
  if (!operationId) throw new Error('restore_operation_required');
  const response = await fetch(workerUrl(`/api/rebuild/memory-restores/${encodeURIComponent(operationId)}/resume`), {
    method: 'POST', headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify({ confirm: true }),
  });
  const payload = await readResponseJson(response);
  if (!response.ok) throw responseFailure('restore_resume_unavailable', 'restore_resume_unavailable', response.status);
  return payload;
}

export function newBackupCommand() {
  return Array.from(globalThis.crypto.getRandomValues(new Uint8Array(16)), value => value.toString(16).padStart(2, '0')).join('');
}

async function backupResponse(response) {
  const payload = await readResponseJson(response);
  if (!response.ok) throw responseFailure(payload.detail || 'backup_request_failed', payload.detail || 'backup_request_failed', response.status);
  return payload;
}

export async function downloadMemorySnapshot({ snapshotId, signal, isCurrent = () => true } = {}) {
  if (!snapshotId) throw new Error('backup_snapshot_required');
  const identity = deviceIdentity();
  const response = await fetch(workerUrl(`${BACKUPS_ENDPOINT}/snapshots/${encodeURIComponent(snapshotId)}/download`),
    { cache: 'no-store', redirect: 'error', signal });
  return downloadResponseBlob(response, `chriptmas-backup-${snapshotId}.zip`,
    () => isDeviceIdentity(identity) && isCurrent(), { signal });
}

export async function importMemorySnapshot({ commandId, file, signal } = {}) {
  if (!commandId || !(file instanceof Blob)) throw new Error('backup_import_required');
  return backupResponse(await fetch(workerUrl(`${BACKUPS_ENDPOINT}/imports/${encodeURIComponent(commandId)}`), {
    method: 'PUT', headers: { Accept: 'application/json', 'Content-Type': 'application/zip' }, body: file, redirect: 'error', signal,
  }));
}

export async function loadBackupImport({ commandId, signal } = {}) {
  if (!commandId) throw new Error('backup_command_required');
  return backupResponse(await fetch(workerUrl(`${BACKUPS_ENDPOINT}/imports/${encodeURIComponent(commandId)}`), {
    headers: { Accept: 'application/json' }, cache: 'no-store', signal,
  }));
}

export async function cancelUnreceivedBackupImport({ commandId, signal } = {}) {
  if (!commandId) throw new Error('backup_command_required');
  return backupResponse(await fetch(workerUrl(`${BACKUPS_ENDPOINT}/imports/${encodeURIComponent(commandId)}/cancel-unreceived`), {
    method: 'POST', headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify({ expected_revision: 0 }), cache: 'no-store', redirect: 'error', signal,
  }));
}

export async function loadBackupSchedule(signal) {
  return backupResponse(await fetch(workerUrl(`${BACKUPS_ENDPOINT}/schedule`), { headers: { Accept: 'application/json' }, cache: 'no-store', signal }));
}

export async function saveBackupSchedule(body, signal) {
  return backupResponse(await fetch(workerUrl(`${BACKUPS_ENDPOINT}/schedule`), {
    method: 'PATCH', headers: { Accept: 'application/json', 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal,
  }));
}

export async function loadBackupScheduleCommand({ commandId, signal } = {}) {
  return backupResponse(await fetch(workerUrl(`${BACKUPS_ENDPOINT}/schedule/commands/${encodeURIComponent(commandId)}`), {
    headers: { Accept: 'application/json' }, cache: 'no-store', signal,
  }));
}
