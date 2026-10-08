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

export const VAULT_STATUS_ENDPOINT = "/api/rebuild/vault-status";
export const STORAGE_GOVERNANCE_ENDPOINT = "/api/rebuild/retention/storage-governance";
export const PROVIDER_BOUNDARY_ENDPOINT = "/api/rebuild/provider-boundary";
export const MEMORY_ASSETS_EXPORT_ENDPOINT = "/api/rebuild/memory-assets/export";
export const MEMORY_SNAPSHOTS_ENDPOINT = "/api/rebuild/memory-snapshots";

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
  if (!response.ok) {
    throw new Error(`准备回滚失败 (${response.status})`);
  }
  return response.json();
}

export async function confirmRestoreSafetyGate({
  snapshotId,
  rollbackId,
  confirm = true,
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
    body: JSON.stringify({ rollback_id: rollbackId, confirm }),
  });
  if (!response.ok) {
    throw new Error(`恢复安全门确认失败 (${response.status})`);
  }
  return response.json();
}
