import { productFetch as fetch } from '../../shared/api/deviceTransport';
import { readResponseJson } from '../../shared/lib/responseJson';
import { libraryBackendUrl } from '../rebuild/libraryOverviewTransport';
import { recognitionApi } from '../../shared/api/recognitionApi';
import { loadCloudAsrProviderSettings, saveCloudAsrProviderSettings, grantCloudAsrEgressConsent, revokeCloudAsrEgressConsent } from '../rebuild/rebuildSettingsApi';
import { subscriptionRequest } from '../../shared/ui/subscriptionApi';
export async function settingsRequest(path, body, method = 'GET', signal) {
  const response = await fetch(libraryBackendUrl(`/api/v2/${path}`), { method, cache: 'no-store', headers: { 'Content-Type': 'application/json' }, ...(body ? { body: JSON.stringify(body) } : {}), ...(signal ? { signal } : {}) });
  const value = await readResponseJson(response);
  if (!response.ok) { const error = new Error(response.status === 409 ? '设置已变化 · 刷新' : '操作未完成 · 重试'); error.code = response.status === 409 ? 'revision_conflict' : 'settings_request_failed'; error.status = response.status; error.detail = typeof value?.detail === 'string' ? value.detail : undefined; throw error; }
  return value;
}
const receiptQuery = values => new URLSearchParams(Object.entries(values).filter(([, value]) => value !== null && value !== undefined && value !== '')).toString();
export const settingsApi = {
  loadAsrLexicon: signal => settingsRequest('asr/lexicon', undefined, 'GET', signal),
  importAsrLexicon: (body, signal) => settingsRequest('asr/lexicon/import', body, 'POST', signal),
  changeAsrLexicon: (body, signal) => settingsRequest('asr/lexicon/changes', body, 'POST', signal),
  integrity: () => settingsRequest('settings/integrity'),
  inspirations: signal => settingsRequest('settings/inspirations', undefined, 'GET', signal),
  saveInspirations: ({ enabled, time, expectedRevision }, signal) => settingsRequest('settings/inspirations',
    { enabled, time, expected_revision: expectedRevision }, 'PATCH', signal),
  signals: () => settingsRequest('settings/signals'),
  saveSignals: (enabled, revision) => settingsRequest('settings/signals', { enabled, expected_revision: revision }, 'PATCH'),
  clearSignals: revision => settingsRequest('settings/signals/clear', { expected_revision: revision }, 'POST'),
  load: () => settingsRequest('settings'), projects: () => settingsRequest('projects'),
  savePrivacy: (ids, revision) => settingsRequest('settings/privacy', { private_projects: ids, expected_revision: revision }, 'PATCH'),
  saveExternalAgent: preferences => settingsRequest('settings/external-agent', preferences, 'PATCH'),
  executorPlugins: signal => settingsRequest('settings/external-executor-plugins', undefined, 'GET', signal),
  saveExecutorPlugins: preferences => settingsRequest('settings/external-executor-plugins', preferences, 'PUT'),
  connection: (projectId, signal) => settingsRequest(`settings/external-agent/connection?project_id=${encodeURIComponent(projectId)}`, undefined, 'GET', signal),
  snapshot: (body, signal) => settingsRequest('settings/external-agent/snapshot', body, 'POST', signal),
  saveExternalProxy: preferences => settingsRequest('settings/external-proxy', preferences, 'PATCH'),
  privateSources: () => settingsRequest('settings/private-sources'), receipts: () => settingsRequest('settings/egress-receipts?limit=50'),
  monthlyUsage: ({ month, timezone, scope = 'egress' } = {}, signal) => settingsRequest(`settings/usage-month?${receiptQuery({ month, timezone, scope })}`, undefined, 'GET', signal),
  receiptHistory: ({ month, timezone, purpose, cursor, limit = 25, scope = 'egress' } = {}, signal) => settingsRequest(`settings/receipt-history?${receiptQuery({ month, timezone, purpose, cursor, limit, scope })}`, undefined, 'GET', signal),
  receiptDetail: (identity, signal, { month, timezone, scope = 'egress' } = {}) => settingsRequest(`settings/receipt-history/${encodeURIComponent(identity)}?${receiptQuery({ month, timezone, scope })}`, undefined, 'GET', signal),
  saveProject: (row, changes) => settingsRequest(`projects/${encodeURIComponent(row.id)}`, { ...changes, expected_revision: row.revision }, 'PATCH'),
  createProject: name => settingsRequest('projects', { name }, 'POST'),
  saveModel: args => settingsRequest('settings/model', args.serverManaged ? { purpose: args.purpose,
    allow_remote: args.allowRemote === true, enabled: args.enabled === true, expected_revision: args.expectedRevision } : { purpose: args.purpose, base_url: args.baseUrl,
    model: args.model, api_key: args.apiKey, allow_remote: args.allowRemote === true,
    enabled: args.enabled === true, expected_revision: args.expectedRevision,
    clear_api_key: args.clearApiKey === true }, 'PUT'),
  saveMode: async ({ clearSubscription, ...args }) => {
    if (clearSubscription) { const status = await subscriptionRequest(); await subscriptionRequest('/selection', { model: null, expected_revision: status.selection.revision }, 'PATCH'); }
    return settingsRequest('settings/generation-mode', { mode: args.mode,
      local_enabled: args.localEnabled === true, local_base_url: args.localBaseUrl,
      expected_revision: args.expectedRevision }, 'PUT');
  }, testModel: recognitionApi.testSettings,
  savePrices: ({ purpose, rates, expectedRevision, expectedConfigurationRevision }) => settingsRequest('settings/model-prices',
    { purpose, rates, expected_revision: expectedRevision, expected_configuration_revision: expectedConfigurationRevision }, 'PATCH'),
  saveVisionMode: ({ mode, expectedRevision }) => settingsRequest('settings/vision-mode',
    { mode, expected_revision: expectedRevision }, 'PATCH'),
  saveEmbeddingMode: ({ mode, expectedRevision }) => settingsRequest('settings/embedding-mode',
    { mode, expected_revision: expectedRevision }, 'PATCH'),
  loadEmbedding: revision => settingsRequest('settings/embedding/load', { expected_revision: revision }, 'POST'),
  unloadEmbedding: revision => settingsRequest('settings/embedding/unload', { expected_revision: revision }, 'POST'),
  installEmbedding: revision => settingsRequest('settings/embedding/install', { expected_revision: revision }, 'POST'),
  saveFastModel: ({ model, expectedRevision, expectedGenerationRevision, expectedModeRevision }) => settingsRequest('settings/fast-model',
    { model, expected_revision: expectedRevision, expected_generation_revision: expectedGenerationRevision, expected_mode_revision: expectedModeRevision }, 'PATCH'),
  fastModels: signal => subscriptionRequest('/models', undefined, 'GET', signal),
  loadConstraints: recognitionApi.loadConstraints, saveConstraint: recognitionApi.saveConstraint,
  saveAsrKey: apiKey => {
    if (typeof globalThis.electronAPI?.captureCredential !== 'function') throw new Error('desktop_capture_required');
    return globalThis.electronAPI.captureCredential('tokenhub_asr_api_key', 'tokenhub-hy-asr', apiKey, `cmd-${crypto.randomUUID()}`);
  },
  saveWebAsrKey: (body, signal) => settingsRequest('settings/asr-key', body, 'PUT', signal),
  asrKeyStatus: (identity, signal) => settingsRequest(`settings/asr-key/operations/${encodeURIComponent(identity)}`, undefined, 'GET', signal),
  cancelAsrKey: (identity, signal) => settingsRequest(`settings/asr-key/operations/${encodeURIComponent(identity)}/cancel`, {}, 'POST', signal),
  purposeTestSample: (purpose, signal) => settingsRequest(`settings/purpose-tests/samples/${encodeURIComponent(purpose)}`, undefined, 'GET', signal),
  runPurposeTest: (body, signal) => settingsRequest('settings/purpose-tests', body, 'POST', signal),
  purposeTestStatus: (identity, signal) => settingsRequest(`settings/purpose-tests/${encodeURIComponent(identity)}`, undefined, 'GET', signal),
  cancelPurposeTest: (identity, signal) => settingsRequest(`settings/purpose-tests/${encodeURIComponent(identity)}/cancel`, {}, 'POST', signal),
  cleanupPurposeTest: (identity, signal) => settingsRequest(`settings/purpose-tests/${encodeURIComponent(identity)}/cleanup`, {}, 'POST', signal),
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
