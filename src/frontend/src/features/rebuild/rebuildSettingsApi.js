import { productFetch as fetch } from '../../shared/api/deviceTransport';
import { assertServerAvailable, readResponseJson } from '../../shared/lib/responseJson';
export const PROVIDERS_ENDPOINT = "/api/providers";
export const FOUR_LAYER_PROVIDER_STATUS_ENDPOINT = "/api/rebuild/providers/deepseek/status";
export const AUTO_MEMORY_PUBLICATION_SETTINGS_ENDPOINT = "/api/rebuild/settings/auto-memory-publication";
export const LOCAL_ASR_PROVIDER_SETTINGS_ENDPOINT = "/api/rebuild/settings/local-asr-provider";
export const CLOUD_ASR_PROVIDER_SETTINGS_ENDPOINT = "/api/rebuild/settings/cloud-asr-provider";
export const REALTIME_ASR_PROVIDER_SETTINGS_ENDPOINT = "/api/rebuild/settings/realtime-asr-provider";
export const REALTIME_ASR_LEXICON_ENDPOINT = "/api/rebuild/settings/realtime-asr-lexicon";
export const LOCAL_DOCUMENT_TEXT_EXTRACTOR_SETTINGS_ENDPOINT = "/api/rebuild/settings/local-document-text-extractor";
export const LOCAL_OCR_PROVIDER_SETTINGS_ENDPOINT = "/api/rebuild/settings/local-ocr-provider";
export const LOCAL_ASR_MODELS_ENDPOINT = "/api/asr/faster-whisper/models";
export const DEVELOPER_STUDIO_CONFIG_ENDPOINT = "/api/rebuild/developer-studio/config";
export const PROMPT_ACTIVATION_ENDPOINT = "/api/rebuild/developer-studio/prompt-activation";
export const DEVELOPER_STUDIO_LOGS_ENDPOINT = "/api/rebuild/developer-studio/logs";
export const DEVELOPER_STUDIO_TEST_LAB_ENDPOINT = "/api/rebuild/developer-studio/test-lab";
export const PROCESSING_RECIPE_REGISTRY_ENDPOINT = "/api/rebuild/developer-studio/processing-recipes";
export const MODEL_ROUTE_MIGRATION_ENDPOINT = "/api/rebuild/model-routes/migration";
export const MODEL_ROUTES_ENDPOINT = "/api/model-routes";
export const MODEL_ROUTE_RUNTIME_ENDPOINT = "/api/model-route-runtime";
export const MEMORY_RETRIEVAL_SETTINGS_ENDPOINT = "/api/rebuild/settings/memory-retrieval";
export const MEMORY_PROJECTION_DIAGNOSTICS_ENDPOINT = "/api/rebuild/developer-studio/memory-projection";
export const MEMORY_PROJECTION_AUTOMATION_ENDPOINT = "/api/rebuild/automations/memory-projection-rebuild";
export const MEMORY_RETRIEVAL_PLAN_PREVIEW_ENDPOINT = "/api/rebuild/developer-studio/memory-retrieval/plan-preview";
export const MEMORY_RETRIEVAL_PERFORMANCE_ENDPOINT = "/api/rebuild/developer-studio/memory-retrieval/performance";

function workerUrl(endpoint) {
  const electronBackend = globalThis.electronAPI?.backendBaseUrl;
  if (electronBackend) {
    return `${electronBackend.replace(/\/$/, "")}${endpoint}`;
  }

  return endpoint;
}

async function jsonRequest(endpoint, { fetchImpl = fetch, errorMessage = "设置暂时无法同步", ...options } = {}) {
  const response = await fetchImpl(workerUrl(endpoint), {
    headers: {
      Accept: "application/json",
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
    ...options,
  });
  assertServerAvailable(response);
  if (!response.ok) {
    const payload = await readResponseJson(response);
    const detail = typeof payload?.detail === "string" ? payload.detail : "";
    const safeDetail = detail
      .replace(/sk-[A-Za-z0-9_-]{8,}/g, "[已隐藏密钥]")
      .replace(/(bearer\s+)[^\s,;]+/gi, "$1[已隐藏凭据]");
    const error = new Error(safeDetail || errorMessage);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  if (response.status === 204) {
    return null;
  }
  return readResponseJson(response);
}

export function listProviders({ fetchImpl = fetch } = {}) {
  return jsonRequest(PROVIDERS_ENDPOINT, { fetchImpl });
}

export function createProvider(payload, { fetchImpl = fetch } = {}) {
  return jsonRequest(PROVIDERS_ENDPOINT, {
    fetchImpl,
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateProvider(providerId, payload, { fetchImpl = fetch } = {}) {
  return jsonRequest(`${PROVIDERS_ENDPOINT}/${encodeURIComponent(providerId)}`, {
    fetchImpl,
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function activateProvider(providerId, { fetchImpl = fetch } = {}) {
  return jsonRequest(`${PROVIDERS_ENDPOINT}/${encodeURIComponent(providerId)}/activate`, {
    fetchImpl,
    method: "POST",
  });
}

export function loadMemoryRetrievalSettings({ projectId = "default", fetchImpl = fetch } = {}) {
  return jsonRequest(
    `${MEMORY_RETRIEVAL_SETTINGS_ENDPOINT}?project_id=${encodeURIComponent(projectId)}`,
    { fetchImpl },
  );
}

export function loadMemoryProjectionDiagnostics({ projectId = "default", fetchImpl = fetch } = {}) {
  return jsonRequest(
    `${MEMORY_PROJECTION_DIAGNOSTICS_ENDPOINT}?project_id=${encodeURIComponent(projectId)}`,
    { fetchImpl },
  );
}

export function previewMemoryProjectionAutomation({ projectId = "default", fetchImpl = fetch } = {}) {
  return jsonRequest(`${MEMORY_PROJECTION_AUTOMATION_ENDPOINT}/preview`, {
    fetchImpl,
    method: "POST",
    body: JSON.stringify({ project_id: projectId }),
    errorMessage: "概况更新预检未完成",
  });
}

export function createMemoryProjectionAutomationGrant(payload, { fetchImpl = fetch } = {}) {
  return jsonRequest(`${MEMORY_PROJECTION_AUTOMATION_ENDPOINT}/grants`, {
    fetchImpl,
    method: "POST",
    body: JSON.stringify({
      project_id: payload.project_id,
      authority_fingerprint: payload.authority_fingerprint,
      expires_at: payload.expires_at,
      command_id: payload.command_id,
    }),
    errorMessage: "本次概况更新授权未创建",
  });
}

export function executeMemoryProjectionAutomationGrant(grantId, payload, { fetchImpl = fetch } = {}) {
  return jsonRequest(`${MEMORY_PROJECTION_AUTOMATION_ENDPOINT}/grants/${encodeURIComponent(grantId)}/execute`, {
    fetchImpl,
    method: "POST",
    body: JSON.stringify({
      project_id: payload.project_id,
      authority_fingerprint: payload.authority_fingerprint,
      expected_grant_revision: payload.expected_grant_revision,
    }),
    errorMessage: "概况更新未能提交",
  });
}

export function loadMemoryProjectionAutomationGrant(grantId, { projectId = "default", fetchImpl = fetch } = {}) {
  return jsonRequest(`${MEMORY_PROJECTION_AUTOMATION_ENDPOINT}/grants/${encodeURIComponent(grantId)}?project_id=${encodeURIComponent(projectId)}`, {
    fetchImpl,
    errorMessage: "概况更新状态暂不可用",
  });
}

export function previewMemoryRetrievalPlan(query, { fetchImpl = fetch } = {}) {
  return jsonRequest(MEMORY_RETRIEVAL_PLAN_PREVIEW_ENDPOINT, {
    fetchImpl,
    method: "POST",
    body: JSON.stringify({ query }),
    errorMessage: "读取计划预览未能生成",
  });
}

export function loadMemoryRetrievalPerformance({ projectId = "default", limit = 100, fetchImpl = fetch } = {}) {
  const params = new URLSearchParams({
    project_id: projectId,
    limit: String(limit),
  });
  return jsonRequest(`${MEMORY_RETRIEVAL_PERFORMANCE_ENDPOINT}?${params.toString()}`, {
    fetchImpl,
    errorMessage: "读取性能统计暂时不可用",
  });
}

export function loadProviderDisconnectPreview(providerId, { fetchImpl = fetch } = {}) {
  return jsonRequest(`${PROVIDERS_ENDPOINT}/${encodeURIComponent(providerId)}/disconnect-preview`, {
    fetchImpl,
    errorMessage: "模型服务依赖暂时无法核对",
  });
}

export function deleteProvider(providerId, { fetchImpl = fetch } = {}) {
  return jsonRequest(`${PROVIDERS_ENDPOINT}/${encodeURIComponent(providerId)}`, {
    fetchImpl,
    method: "DELETE",
  });
}

export function grantProviderEgressConsent(providerId, manifestId, { fetchImpl = fetch } = {}) {
  return jsonRequest(`${PROVIDERS_ENDPOINT}/${encodeURIComponent(providerId)}/egress-consent`, {
    fetchImpl,
    method: "POST",
    body: JSON.stringify({ manifest_id: manifestId, confirm: true }),
  });
}

export function revokeProviderEgressConsent(providerId, { fetchImpl = fetch } = {}) {
  return jsonRequest(`${PROVIDERS_ENDPOINT}/${encodeURIComponent(providerId)}/egress-consent`, {
    fetchImpl,
    method: "DELETE",
  });
}

export function deleteProviderSecret(providerId, { fetchImpl = fetch } = {}) {
  return jsonRequest(`${PROVIDERS_ENDPOINT}/${encodeURIComponent(providerId)}/secret`, {
    fetchImpl,
    method: "DELETE",
  });
}

export function testProvider(providerId, payload = {}, { fetchImpl = fetch } = {}) {
  return jsonRequest(`${PROVIDERS_ENDPOINT}/${encodeURIComponent(providerId)}/test`, {
    fetchImpl,
    method: "POST",
    body: JSON.stringify({
      llm_provider: payload.llm_provider,
      openai_base_url: payload.base_url,
      openai_model: payload.model,
    }),
  });
}

export function listProviderModels(providerId, { fetchImpl = fetch } = {}) {
  return jsonRequest(`${PROVIDERS_ENDPOINT}/${encodeURIComponent(providerId)}/models`, {
    fetchImpl,
  });
}

export function loadFourLayerProviderStatus({ fetchImpl = fetch } = {}) {
  return jsonRequest(FOUR_LAYER_PROVIDER_STATUS_ENDPOINT, { fetchImpl });
}

export function loadAutoMemoryPublicationSettings({ fetchImpl = fetch } = {}) {
  return jsonRequest(AUTO_MEMORY_PUBLICATION_SETTINGS_ENDPOINT, { fetchImpl });
}

export function saveAutoMemoryPublicationSettings(payload, { fetchImpl = fetch } = {}) {
  return jsonRequest(AUTO_MEMORY_PUBLICATION_SETTINGS_ENDPOINT, {
    fetchImpl,
    method: "PUT",
    body: JSON.stringify({
      enabled: payload.enabled === true,
      confirm_enable: payload.enabled === true,
      allowed_layers: payload.allowed_layers,
    }),
  });
}

export function loadLocalAsrProviderSettings({ fetchImpl = fetch } = {}) {
  return jsonRequest(LOCAL_ASR_PROVIDER_SETTINGS_ENDPOINT, { fetchImpl });
}

export function listLocalAsrModels({ fetchImpl = fetch } = {}) {
  return jsonRequest(LOCAL_ASR_MODELS_ENDPOINT, { fetchImpl });
}

export function downloadLocalAsrModel(modelId, { fetchImpl = fetch } = {}) {
  return jsonRequest(`${LOCAL_ASR_MODELS_ENDPOINT}/${encodeURIComponent(modelId)}/download`, {
    fetchImpl,
    method: "POST",
  });
}

export function saveLocalAsrProviderSettings(payload, { fetchImpl = fetch } = {}) {
  return jsonRequest(LOCAL_ASR_PROVIDER_SETTINGS_ENDPOINT, {
    fetchImpl,
    method: "PUT",
    body: JSON.stringify({
      enabled: payload.enabled === true,
      provider_name: payload.provider_name || "local-command-asr",
      command: Array.isArray(payload.command) ? payload.command : [],
      model_profile: payload.model_profile || "large-v3-turbo",
      model_name: payload.model_name || payload.model_profile || "large-v3-turbo",
      confirm_enable: payload.enabled === true,
    }),
  });
}

export function loadDeveloperStudioConfig({ fetchImpl = fetch } = {}) {
  return jsonRequest(DEVELOPER_STUDIO_CONFIG_ENDPOINT, { fetchImpl });
}

export function saveDeveloperStudioConfig(payload, { fetchImpl = fetch } = {}) {
  return jsonRequest(DEVELOPER_STUDIO_CONFIG_ENDPOINT, {
    fetchImpl,
    errorMessage: "Developer Studio 配置暂时无法同步",
    method: "PUT",
    body: JSON.stringify({
      expected_revision: Number.isInteger(payload.expected_revision) ? payload.expected_revision : undefined,
      model_profiles: Array.isArray(payload.model_profiles) ? payload.model_profiles : [],
      task_model_map: payload.task_model_map && typeof payload.task_model_map === "object" ? payload.task_model_map : {},
      prompts: Array.isArray(payload.prompts) ? payload.prompts : [],
      skills: Array.isArray(payload.skills) ? payload.skills : [],
      workflow_steps: Array.isArray(payload.workflow_steps) ? payload.workflow_steps : [],
      snapshots: Array.isArray(payload.snapshots) ? payload.snapshots : [],
    }),
  });
}

export function loadCloudAsrProviderSettings({ fetchImpl = fetch } = {}) {
  return jsonRequest(CLOUD_ASR_PROVIDER_SETTINGS_ENDPOINT, { fetchImpl });
}

export function saveCloudAsrProviderSettings(payload, { fetchImpl = fetch } = {}) {
  const enabled = payload?.enabled === true;
  return jsonRequest(CLOUD_ASR_PROVIDER_SETTINGS_ENDPOINT, {
    fetchImpl,
    method: "PUT",
    body: JSON.stringify({ enabled, confirm_enable: enabled }),
    errorMessage: "云端语音识别设置未能保存",
  });
}

export function grantCloudAsrEgressConsent(manifestId, { fetchImpl = fetch } = {}) {
  return jsonRequest(`${CLOUD_ASR_PROVIDER_SETTINGS_ENDPOINT}/egress-consent`, {
    fetchImpl,
    method: "POST",
    body: JSON.stringify({ manifest_id: manifestId, confirm: true }),
    errorMessage: "音频发送授权未能保存",
  });
}

export function revokeCloudAsrEgressConsent({ fetchImpl = fetch } = {}) {
  return jsonRequest(`${CLOUD_ASR_PROVIDER_SETTINGS_ENDPOINT}/egress-consent`, {
    fetchImpl,
    method: "DELETE",
    errorMessage: "音频发送授权未能撤销",
  });
}

export function loadRealtimeAsrProviderSettings({ fetchImpl = fetch } = {}) {
  return jsonRequest(REALTIME_ASR_PROVIDER_SETTINGS_ENDPOINT, { fetchImpl });
}

export function saveRealtimeAsrProviderSettings(payload, { fetchImpl = fetch } = {}) {
  const enabled = payload?.enabled === true;
  return jsonRequest(REALTIME_ASR_PROVIDER_SETTINGS_ENDPOINT, {
    fetchImpl,
    method: "PUT",
    body: JSON.stringify({
      enabled,
      confirm_enable: enabled,
      endpoint: payload?.endpoint || undefined,
      region: payload?.workspace_id ? payload?.region : undefined,
      workspace_id: payload?.workspace_id || undefined,
    }),
    errorMessage: "实时语音识别设置未能保存",
  });
}

export function grantRealtimeAsrEgressConsent(manifestId, { fetchImpl = fetch } = {}) {
  return jsonRequest(`${REALTIME_ASR_PROVIDER_SETTINGS_ENDPOINT}/egress-consent`, {
    fetchImpl,
    method: "POST",
    body: JSON.stringify({ manifest_id: manifestId, confirm: true }),
    errorMessage: "实时麦克风音频发送授权未能保存",
  });
}

export function revokeRealtimeAsrEgressConsent({ fetchImpl = fetch } = {}) {
  return jsonRequest(`${REALTIME_ASR_PROVIDER_SETTINGS_ENDPOINT}/egress-consent`, {
    fetchImpl,
    method: "DELETE",
    errorMessage: "实时麦克风音频发送授权未能撤销",
  });
}

export function loadRealtimeAsrLexicon({ fetchImpl = fetch } = {}) {
  return jsonRequest(REALTIME_ASR_LEXICON_ENDPOINT, { fetchImpl });
}

export function upsertRealtimeAsrTerm(payload, { fetchImpl = fetch } = {}) {
  return jsonRequest(`${REALTIME_ASR_LEXICON_ENDPOINT}/terms`, {
    fetchImpl,
    method: "POST",
    body: JSON.stringify({
      term: payload?.term || "",
      weight: payload?.weight ?? 4,
      is_super: payload?.is_super === true,
    }),
    errorMessage: "专有名词未能保存",
  });
}

export function retireRealtimeAsrTerm(termId, { fetchImpl = fetch } = {}) {
  return jsonRequest(`${REALTIME_ASR_LEXICON_ENDPOINT}/terms/${encodeURIComponent(termId)}/retire`, {
    fetchImpl,
    method: "POST",
    errorMessage: "专有名词未能停用",
  });
}

export function proposeRealtimeAsrCandidate(payload, { fetchImpl = fetch } = {}) {
  return jsonRequest(`${REALTIME_ASR_LEXICON_ENDPOINT}/candidates`, {
    fetchImpl,
    method: "POST",
    body: JSON.stringify({
      term: payload?.term || "",
      suggested_weight: payload?.suggested_weight ?? 4,
      source_kind: payload?.source_kind || "explicit_correction",
      source_ref: payload?.source_ref || "workbench-user-correction",
      confirm_source: true,
    }),
    errorMessage: "纠错候选未能保存",
  });
}

export function reviewRealtimeAsrCandidate(candidateId, payload, { fetchImpl = fetch } = {}) {
  return jsonRequest(`${REALTIME_ASR_LEXICON_ENDPOINT}/candidates/${encodeURIComponent(candidateId)}/review`, {
    fetchImpl,
    method: "POST",
    body: JSON.stringify({
      action: payload?.action,
      weight: payload?.weight,
      is_super: payload?.is_super === true,
    }),
    errorMessage: "专名候选审核未能完成",
  });
}

export function loadLocalDocumentTextExtractorSettings({ fetchImpl = fetch } = {}) {
  return jsonRequest(LOCAL_DOCUMENT_TEXT_EXTRACTOR_SETTINGS_ENDPOINT, { fetchImpl });
}

export function saveLocalDocumentTextExtractorSettings(payload, { fetchImpl = fetch } = {}) {
  const enabled = payload.enabled === true;
  return jsonRequest(LOCAL_DOCUMENT_TEXT_EXTRACTOR_SETTINGS_ENDPOINT, {
    fetchImpl,
    method: "PUT",
    body: JSON.stringify({
      enabled,
      provider_name: "builtin-document-text",
      command: ["builtin:document-text"],
      confirm_enable: enabled,
    }),
  });
}

export function loadLocalOcrProviderSettings({ fetchImpl = fetch } = {}) {
  return jsonRequest(LOCAL_OCR_PROVIDER_SETTINGS_ENDPOINT, { fetchImpl });
}

export function saveBuiltinWindowsOcrSettings(payload, { fetchImpl = fetch } = {}) {
  const enabled = payload?.enabled === true;
  return jsonRequest(LOCAL_OCR_PROVIDER_SETTINGS_ENDPOINT, {
    fetchImpl,
    method: "PUT",
    body: JSON.stringify({
      enabled,
      provider_name: "builtin-windows-ocr",
      command: ["builtin:windows-ocr"],
      confirm_enable: enabled,
    }),
  });
}

export function loadPromptActivation({ fetchImpl = fetch } = {}) {
  return jsonRequest(PROMPT_ACTIVATION_ENDPOINT, {
    fetchImpl,
    errorMessage: "提示词生产状态暂时不可用",
  });
}

export function previewPromptActivation(payload, { fetchImpl = fetch } = {}) {
  return jsonRequest(`${PROMPT_ACTIVATION_ENDPOINT}/preview`, {
    fetchImpl,
    method: "POST",
    body: JSON.stringify({
      unit_id: payload.unit_id,
      expected_config_revision: payload.expected_config_revision,
      expected_activation_revision: payload.expected_activation_revision,
    }),
    errorMessage: "提示词激活预检未完成",
  });
}

export function activatePromptUnit(payload, { fetchImpl = fetch } = {}) {
  return jsonRequest(`${PROMPT_ACTIVATION_ENDPOINT}/activate`, {
    fetchImpl,
    method: "POST",
    body: JSON.stringify({
      unit_id: payload.unit_id,
      expected_config_revision: payload.expected_config_revision,
      expected_activation_revision: payload.expected_activation_revision,
      preview_token: payload.preview_token,
      confirm: payload.confirm === true,
      reason: payload.reason,
    }),
    errorMessage: "提示词激活未完成",
  });
}

export function rollbackPromptUnit(payload, { fetchImpl = fetch } = {}) {
  return jsonRequest(`${PROMPT_ACTIVATION_ENDPOINT}/rollback`, {
    fetchImpl,
    method: "POST",
    body: JSON.stringify({
      unit_id: payload.unit_id,
      expected_config_revision: payload.expected_config_revision,
      expected_activation_revision: payload.expected_activation_revision,
      confirm: payload.confirm === true,
      reason: payload.reason,
    }),
    errorMessage: "提示词生产版本回滚未完成",
  });
}

export function previewModelRouteMigration(rendererTaskMap = {}, { fetchImpl = fetch } = {}) {
  return jsonRequest(`${MODEL_ROUTE_MIGRATION_ENDPOINT}/preview`, {
    fetchImpl,
    method: "POST",
    body: JSON.stringify({ renderer_task_map: rendererTaskMap }),
    errorMessage: "旧模型配置迁移预览暂时不可用",
  });
}

export function confirmModelRouteMigration(payload, { fetchImpl = fetch } = {}) {
  return jsonRequest(`${MODEL_ROUTE_MIGRATION_ENDPOINT}/confirm`, {
    fetchImpl,
    method: "POST",
    body: JSON.stringify({
      preview_token: payload.preview_token,
      confirm: payload.confirm === true,
      choices: payload.choices || {},
      renderer_task_map: payload.renderer_task_map || {},
    }),
    errorMessage: "旧模型配置迁移未完成",
  });
}

export function rollbackModelRouteMigration(migrationId, payload, { fetchImpl = fetch } = {}) {
  return jsonRequest(
    `/api/rebuild/model-routes/migrations/${encodeURIComponent(migrationId)}/rollback`,
    {
      fetchImpl,
      method: "POST",
      body: JSON.stringify({
        expected_registry_revision: payload.expected_registry_revision,
        confirm: payload.confirm === true,
      }),
      errorMessage: "模型路由迁移回滚未完成",
    },
  );
}

export function listModelRoutes({ fetchImpl = fetch } = {}) {
  return jsonRequest(MODEL_ROUTES_ENDPOINT, { fetchImpl, errorMessage: "模型路由暂时不可用" });
}

export function updateModelRouteBatch(payload, { fetchImpl = fetch } = {}) {
  return jsonRequest(`${MODEL_ROUTES_ENDPOINT}/batch`, {
    fetchImpl,
    method: "PUT",
    body: JSON.stringify({
      expected_registry_revision: payload.expected_registry_revision,
      assignments: payload.assignments,
    }),
    errorMessage: "模型分工方案未能保存",
  });
}

export function updateModelRoute(routeKey, payload, { fetchImpl = fetch } = {}) {
  return jsonRequest(`${MODEL_ROUTES_ENDPOINT}/${encodeURIComponent(routeKey)}`, {
    fetchImpl,
    method: "PUT",
    body: JSON.stringify(payload),
    errorMessage: "模型路由未能保存",
  });
}

export function loadModelRouteRuntime({ fetchImpl = fetch } = {}) {
  return jsonRequest(MODEL_ROUTE_RUNTIME_ENDPOINT, { fetchImpl, errorMessage: "模型路由运行状态暂时不可用" });
}

export function previewModelRouteRuntime(routeKeys = ["intake.classification"], { fetchImpl = fetch } = {}) {
  return jsonRequest(`${MODEL_ROUTE_RUNTIME_ENDPOINT}/preview`, {
    fetchImpl,
    method: "POST",
    body: JSON.stringify({ route_keys: routeKeys }),
    errorMessage: "模型路由启用预检未完成",
  });
}

export function activateModelRouteRuntime(payload, { fetchImpl = fetch } = {}) {
  return jsonRequest(`${MODEL_ROUTE_RUNTIME_ENDPOINT}/activate`, {
    fetchImpl,
    method: "POST",
    body: JSON.stringify({
      shadow_token: payload.shadow_token,
      route_keys: payload.route_keys,
      expected_runtime_revision: payload.expected_runtime_revision,
      confirm: true,
    }),
    errorMessage: "模型路由未能启用",
  });
}

export function deactivateModelRouteRuntime(runtimeRevision, { fetchImpl = fetch } = {}) {
  return jsonRequest(`${MODEL_ROUTE_RUNTIME_ENDPOINT}/deactivate`, {
    fetchImpl,
    method: "POST",
    body: JSON.stringify({ expected_runtime_revision: runtimeRevision, confirm: true }),
    errorMessage: "模型路由未能停用",
  });
}

/**
 * 加载 Developer Studio 诊断日志（HTTP access / Job execution / LLM call 三类聚合）。
 *
 * @param {object} options
 * @param {string} [options.component] - all|http|job|llm，默认 all
 * @param {string} [options.status]    - all|ok|fail，默认 all
 * @param {number} [options.limit]     - 1..100，默认 50
 * @param {number} [options.offset]    - >= 0，默认 0
 * @param {typeof fetch} [options.fetchImpl]
 * @returns {Promise<{logs: Array, total: number, filters: object}>}
 */
export async function fetchDeveloperStudioLogs(
  { component = "all", status = "all", limit = 50, offset = 0, fetchImpl = fetch } = {},
) {
  const params = new URLSearchParams({
    component: String(component || "all"),
    status: String(status || "all"),
    limit: String(Number.isInteger(limit) && limit > 0 ? limit : 50),
    offset: String(Number.isInteger(offset) && offset >= 0 ? offset : 0),
  });
  return jsonRequest(`${DEVELOPER_STUDIO_LOGS_ENDPOINT}?${params.toString()}`, { fetchImpl });
}

/**
 * 调用 Developer Studio 测试实验室端点，发起一次真实 LLM 测试。
 *
 * @param {object} payload
 * @param {string} payload.test_type - prompt|recipe|pipeline|video|search
 * @param {string} payload.input     - 测试输入内容
 * @param {string} [payload.prompt_id]       - Developer Studio 中的 prompt id
 * @param {typeof fetch} [fetchImpl]
 * @returns {Promise<{test_type, raw, parsed, valid, elapsed_ms, usage, error, model, resolved}>}
 */
export async function runDeveloperStudioTestLab(payload, { fetchImpl = fetch } = {}) {
  return jsonRequest(DEVELOPER_STUDIO_TEST_LAB_ENDPOINT, {
    fetchImpl,
    method: "POST",
    body: JSON.stringify(payload || {}),
  });
}

export async function loadProcessingRecipeRegistry({ fetchImpl = fetch } = {}) {
  return jsonRequest(PROCESSING_RECIPE_REGISTRY_ENDPOINT, { fetchImpl });
}
