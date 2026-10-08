import { productFetch as fetch } from '../../shared/api/deviceTransport';
import { readResponseJson } from '../../shared/lib/responseJson';
import { libraryBackendUrl } from '../rebuild/libraryOverviewTransport';
import { recognitionApi } from '../../shared/api/recognitionApi';
import { loadCloudAsrProviderSettings, saveCloudAsrProviderSettings, grantCloudAsrEgressConsent, revokeCloudAsrEgressConsent } from '../rebuild/rebuildSettingsApi';
import { subscriptionRequest } from '../../shared/ui/subscriptionApi';
export async function settingsRequest(path, body, method = 'GET', signal) {
  const response = await fetch(libraryBackendUrl(`/api/v2/${path}`), { method, cache: 'no-store', headers: { 'Content-Type': 'application/json' }, ...(body ? { body: JSON.stringify(body) } : {}), ...(signal ? { signal } : {}) });
  const value = await readResponseJson(response);
  if (!response.ok) { const error = new Error(response.status === 409 ? '设置已变化 · 刷新' : '操作未完成 · 重试'); error.code = response.status === 409 ? 'revision_conflict' : 'settings_request_failed'; error.status = response.status; throw error; }
  return value;
}
export const settingsApi = {
  integrity: () => settingsRequest('settings/integrity'),
  signals: () => settingsRequest('settings/signals'),
  saveSignals: (enabled, revision) => settingsRequest('settings/signals', { enabled, expected_revision: revision }, 'PATCH'),
  clearSignals: revision => settingsRequest('settings/signals/clear', { expected_revision: revision }, 'POST'),
  load: () => settingsRequest('settings'), projects: () => settingsRequest('projects'),
  savePrivacy: (ids, revision) => settingsRequest('settings/privacy', { private_projects: ids, expected_revision: revision }, 'PATCH'),
  saveExternalAgent: preferences => settingsRequest('settings/external-agent', preferences, 'PATCH'),
  connection: (projectId, signal) => settingsRequest(`settings/external-agent/connection?project_id=${encodeURIComponent(projectId)}`, undefined, 'GET', signal),
  snapshot: (body, signal) => settingsRequest('settings/external-agent/snapshot', body, 'POST', signal),
  saveExternalProxy: preferences => settingsRequest('settings/external-proxy', preferences, 'PATCH'),
  privateSources: () => settingsRequest('settings/private-sources'), receipts: () => settingsRequest('settings/egress-receipts?limit=50'),
  saveProject: (row, changes) => settingsRequest(`projects/${encodeURIComponent(row.id)}`, { ...changes, expected_revision: row.revision }, 'PATCH'),
  createProject: name => settingsRequest('projects', { name }, 'POST'),
  saveModel: recognitionApi.saveSettings, saveMode: async ({ clearSubscription, ...args }) => {
    if (clearSubscription) { const status = await subscriptionRequest(); await subscriptionRequest('/selection', { model: null, expected_revision: status.selection.revision }, 'PATCH'); }
    return recognitionApi.saveGenerationMode(args);
  }, testModel: recognitionApi.testSettings,
  savePrices: ({ purpose, rates, expectedRevision, expectedConfigurationRevision }) => settingsRequest('settings/model-prices',
    { purpose, rates, expected_revision: expectedRevision, expected_configuration_revision: expectedConfigurationRevision }, 'PATCH'),
  saveVisionMode: ({ mode, expectedRevision }) => settingsRequest('settings/vision-mode',
    { mode, expected_revision: expectedRevision }, 'PATCH'),
  saveEmbeddingMode: ({ mode, expectedRevision }) => settingsRequest('settings/embedding-mode',
    { mode, expected_revision: expectedRevision }, 'PATCH'),
  installEmbedding: revision => settingsRequest('settings/embedding/install', { expected_revision: revision }, 'POST'),
  saveFastModel: ({ model, expectedRevision, expectedGenerationRevision, expectedModeRevision }) => settingsRequest('settings/fast-model',
    { model, expected_revision: expectedRevision, expected_generation_revision: expectedGenerationRevision, expected_mode_revision: expectedModeRevision }, 'PATCH'),
  fastModels: signal => subscriptionRequest('/models', undefined, 'GET', signal),
  loadConstraints: recognitionApi.loadConstraints, saveConstraint: recognitionApi.saveConstraint,
  saveAsrKey: apiKey => {
    if (typeof globalThis.electronAPI?.captureCredential !== 'function') throw new Error('desktop_capture_required');
    return globalThis.electronAPI.captureCredential('tokenhub_asr_api_key', 'tokenhub-hy-asr', apiKey, `cmd-${crypto.randomUUID()}`);
  },
  disableAsr: revokeCloudAsrEgressConsent,
  enableAsr: async () => { await saveCloudAsrProviderSettings({ enabled: true }); const settings = await loadCloudAsrProviderSettings(); return grantCloudAsrEgressConsent(settings.egress_manifest.manifest_id); },
  toggleSubscription: async (enabled, revision) => {
    const status = await subscriptionRequest();
    return subscriptionRequest('/selection', { model: status.selection.model || null, expected_revision: status.selection.revision, allow_remote: enabled, expected_generation_revision: revision }, 'PATCH');
  },
  cancelPrivate: async row => {
    if (Number.isInteger(row.source_revision) && Number.isInteger(row.policy_revision)) return recognitionApi.saveSourcePolicy({ projectId: row.project_id, sourceType: row.type, sourceId: row.source_id, expectedSourceRevision: row.source_revision, expectedPolicyRevision: row.policy_revision, allowedPurposes: ['generation', 'embedding', 'rerank'] });
    const data = await recognitionApi.loadWorkbench({ projectId: row.project_id });
    const source = (row.type === 'recognition' ? data.recognitions : data.experiences)?.find(item => item.id === row.source_id);
    if (!source || !Number.isInteger(source.revision)) throw new Error('source_revision_unavailable');
    const snapshot = await recognitionApi.loadSourcePolicy({ projectId: row.project_id, sourceType: row.type, sourceId: row.source_id, revision: source.revision });
    const root = snapshot.nodes?.find(item => item.type === row.type && item.id === row.source_id);
    if (!root || root.policy_revision !== row.policy_revision) throw new Error('revision_conflict');
    return recognitionApi.saveSourcePolicy({ projectId: row.project_id, sourceType: row.type, sourceId: row.source_id, expectedSourceRevision: source.revision, expectedPolicyRevision: row.policy_revision, allowedPurposes: ['generation', 'embedding', 'rerank'] });
  },
};
