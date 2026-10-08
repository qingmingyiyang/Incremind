import { productFetch as fetch } from './deviceTransport';
import { assertServerAvailable, readResponseJson, responseFailure } from '../lib/responseJson';
const API_PREFIX = "/api/recognition";

function backendUrl(endpoint) {
  const base = globalThis.electronAPI?.backendBaseUrl
    || ("");
  return `${String(base).replace(/\/$/, "")}${endpoint}`;
}

async function request(endpoint, { fetchImpl = fetch, ...init } = {}) {
  const response = await fetchImpl(backendUrl(endpoint), {
    headers: { Accept: "application/json", ...(init.body ? { "Content-Type": "application/json" } : {}), ...init.headers },
    ...init,
  });
  assertServerAvailable(response);
  if (!response.ok) {
    let detail = await response.text().catch(() => "");
    try { const parsed = JSON.parse(detail); detail = typeof parsed.detail === "string" ? parsed.detail : ""; } catch { throw responseFailure("invalid_response", "读取未完成 · 重试", response.status); }
    if (detail.startsWith("model_output_incomplete")) detail = "模型输出未完成，未保存为成果或候选。请缩小任务后重试。";
    if (detail.startsWith("project constraints changed")) detail = "项目约束已变化或到期，请重新预览上下文。";
    if (detail.startsWith("selected_context_exceeds_input_capacity")) detail = "认识、项目约束与任务合计超出上下文预算，请精简后重新预览。";
    if (detail.startsWith("source policy is unavailable") || detail.startsWith("source is unavailable")) detail = "该来源的授权状态当前不可用，无法在工作台编辑。请刷新来源后重试。";
    if (detail.startsWith("source policy cannot broaden")) detail = "派生认识只能收窄上游来源的授权，不能新增上游未允许的用途。";
    if (detail.startsWith("source egress is not authorized")) detail = "当前来源未授权向该模型服务发送内容。";
    if (detail.startsWith("source egress snapshot conflicted") || detail.startsWith("source provenance revision conflicted")) detail = "来源或授权已变化，请重新读取授权状态。";
    throw new RecognitionApiError(response.status, detail || `认识服务请求失败：${response.status}`);
  }
  if (response.status === 204) return null;
  return readResponseJson(response);
}

export class RecognitionApiError extends Error {
  constructor(status, message) {
    super(message);
    this.name = "RecognitionApiError";
    this.status = status;
  }
}

export const recognitionApi = Object.freeze({
  setRecallPreference: ({ projectId, recognitionId, expectedRevision, expectedPreferenceRevision, state, fetchImpl } = {}) => request(`${API_PREFIX}/recognitions/${encodeURIComponent(recognitionId)}/recall`, {
    fetchImpl, method: "PATCH", body: JSON.stringify({ project_id: projectId, expected_revision: expectedRevision, expected_preference_revision: expectedPreferenceRevision, state }),
  }),
  loadRecognitionVersions: ({ projectId, recognitionId, fetchImpl } = {}) => request(`${API_PREFIX}/recognitions/${encodeURIComponent(recognitionId)}/versions?project_id=${encodeURIComponent(projectId)}`, { fetchImpl }),
  previewErasure: ({ projectId, recognitionId, expectedRevision, fetchImpl } = {}) => request(`${API_PREFIX}/recognitions/${encodeURIComponent(recognitionId)}/erase-preview`, {
    fetchImpl, method: "POST", body: JSON.stringify({ project_id: projectId, expected_revision: expectedRevision }),
  }),
  eraseRecognition: ({ projectId, recognitionId, expectedRevision, previewId, fetchImpl } = {}) => request(`${API_PREFIX}/recognitions/${encodeURIComponent(recognitionId)}/erase`, {
    fetchImpl, method: "POST", body: JSON.stringify({ project_id: projectId, expected_revision: expectedRevision, preview_id: previewId, confirm: recognitionId }),
  }),
  loadWorkbench: ({ projectId, fetchImpl } = {}) => request(
    `${API_PREFIX}/workbench?project_id=${encodeURIComponent(projectId || "default")}`,
    { fetchImpl },
  ),
  loadGraph: ({ projectId, focus, offset = 0, limit = 40, fetchImpl } = {}) => {
    const query = new URLSearchParams({ project_id: projectId || "default", offset: String(offset), limit: String(limit) });
    if (focus) query.set("focus", focus);
    return request(`${API_PREFIX}/graph?${query.toString()}`, { fetchImpl });
  },
  listGraphViews: ({ projectId, fetchImpl } = {}) => request(
    `${API_PREFIX}/graph-views?project_id=${encodeURIComponent(projectId || "default")}`,
    { fetchImpl },
  ),
  loadGraphView: ({ projectId, viewId, fetchImpl } = {}) => request(
    `${API_PREFIX}/graph-views/${encodeURIComponent(viewId)}?project_id=${encodeURIComponent(projectId || "default")}`,
    { fetchImpl },
  ),
  saveGraphView: ({ projectId, viewId, expectedRevision, nodeIds, positions, collapsedIds, hiddenIds, selectedIds, focusId, fetchImpl } = {}) => request(
    `${API_PREFIX}/graph-views/${encodeURIComponent(viewId)}`,
    { fetchImpl, method: "PUT", body: JSON.stringify({ project_id: projectId, expected_revision: expectedRevision, node_ids: nodeIds, positions, collapsed_ids: collapsedIds, hidden_ids: hiddenIds, selected_ids: selectedIds, focus_id: focusId || null }) },
  ),
  loadTask: ({ projectId, taskId, fetchImpl } = {}) => request(
    `${API_PREFIX}/tasks/${encodeURIComponent(taskId)}?project_id=${encodeURIComponent(projectId || "default")}`,
    { fetchImpl },
  ),
  createExperience: ({ projectId, content, sourceRefs, fetchImpl } = {}) => request(`${API_PREFIX}/experiences`, {
    fetchImpl,
    method: "POST",
    body: JSON.stringify({ project_id: projectId, content, source_refs: sourceRefs }),
  }),
  loadSourcePolicy: ({ projectId, sourceType, sourceId, revision, fetchImpl } = {}) => request(
    `${API_PREFIX}/source-policies/${encodeURIComponent(sourceType)}/${encodeURIComponent(sourceId)}?project_id=${encodeURIComponent(projectId || "default")}&revision=${encodeURIComponent(revision)}`,
    { fetchImpl },
  ),
  saveSourcePolicy: ({ projectId, sourceType, sourceId, expectedSourceRevision, expectedPolicyRevision, allowedPurposes, fetchImpl } = {}) => request(
    `${API_PREFIX}/source-policies/${encodeURIComponent(sourceType)}/${encodeURIComponent(sourceId)}`,
    { fetchImpl, method: "PUT", body: JSON.stringify({ project_id: projectId, expected_source_revision: expectedSourceRevision, expected_policy_revision: expectedPolicyRevision, allowed_purposes: allowedPurposes }) },
  ),
  reviewCandidate: ({ candidateId, projectId, expectedRevision, decision, content, fetchImpl } = {}) => request(`${API_PREFIX}/candidates/${encodeURIComponent(candidateId)}`, {
    fetchImpl,
    method: "PATCH",
    body: JSON.stringify({ project_id: projectId, expected_revision: expectedRevision, decision, ...(content === undefined ? {} : { content }) }),
  }),
  editCandidate: ({ candidateId, projectId, expectedRevision, content, conditions, fetchImpl } = {}) => request(`${API_PREFIX}/candidates/${encodeURIComponent(candidateId)}/draft`, {
    fetchImpl, method: "PATCH",
    body: JSON.stringify({ project_id: projectId, expected_revision: expectedRevision, content, conditions }),
  }),
  previewMarkdown: ({ recognitionId, projectId, markdown, expectedRevision, fetchImpl } = {}) => request(`${API_PREFIX}/recognitions/${encodeURIComponent(recognitionId)}/markdown`, {
    fetchImpl,
    method: "POST",
    body: JSON.stringify({ mode: "preview", project_id: projectId, markdown, expected_revision: expectedRevision }),
  }),
  commitMarkdown: ({ recognitionId, projectId, markdown, expectedRevision, fetchImpl } = {}) => request(`${API_PREFIX}/recognitions/${encodeURIComponent(recognitionId)}/markdown`, {
    fetchImpl,
    method: "POST",
    body: JSON.stringify({ mode: "commit", project_id: projectId, markdown, expected_revision: expectedRevision }),
  }),
  revokeRecognition: ({ recognitionId, projectId, expectedRevision, fetchImpl } = {}) => request(`${API_PREFIX}/recognitions/${encodeURIComponent(recognitionId)}`, {
    fetchImpl,
    method: "DELETE",
    body: JSON.stringify({ project_id: projectId, expected_revision: expectedRevision, mode: "revoke" }),
  }),
  exportMarkdownUrl: (recognitionId, projectId) => backendUrl(`${API_PREFIX}/recognitions/${encodeURIComponent(recognitionId)}/markdown?project_id=${encodeURIComponent(projectId)}`),
  splitRecognition: ({ recognitionId, projectId, expectedRevision, parts, fetchImpl } = {}) => request(`${API_PREFIX}/recognitions/${encodeURIComponent(recognitionId)}/split`, {
    fetchImpl,
    method: "POST",
    body: JSON.stringify({ project_id: projectId, expected_revision: expectedRevision, parts }),
  }),
  mergeRecognitions: ({ projectId, expectedRevisions, content, conditions, fetchImpl } = {}) => request(`${API_PREFIX}/recognitions/merge`, {
    fetchImpl,
    method: "POST",
    body: JSON.stringify({ project_id: projectId, expected_revisions: expectedRevisions, content, conditions }),
  }),
  listRestructureProposals: ({ projectId, fetchImpl } = {}) => request(
    `${API_PREFIX}/restructure-proposals?project_id=${encodeURIComponent(projectId || "default")}`,
    { fetchImpl },
  ),
  reviewRestructureProposal: ({ proposalId, projectId, expectedRevision, decision, fetchImpl } = {}) => request(`${API_PREFIX}/restructure-proposals/${encodeURIComponent(proposalId)}`, {
    fetchImpl,
    method: "PATCH",
    body: JSON.stringify({ project_id: projectId, expected_revision: expectedRevision, decision }),
  }),
  listRelationProposals: ({ projectId, fetchImpl } = {}) => request(
    `${API_PREFIX}/relation-proposals?project_id=${encodeURIComponent(projectId || "default")}`,
    { fetchImpl },
  ),
  proposeRelation: ({ projectId, fromId, toId, relation, evidence, fetchImpl } = {}) => request(`${API_PREFIX}/relation-proposals`, {
    fetchImpl,
    method: "POST",
    body: JSON.stringify({ project_id: projectId, from_id: fromId, to_id: toId, relation, evidence }),
  }),
  reviewRelationProposal: ({ proposalId, projectId, expectedRevision, decision, fetchImpl } = {}) => request(`${API_PREFIX}/relation-proposals/${encodeURIComponent(proposalId)}`, {
    fetchImpl,
    method: "PATCH",
    body: JSON.stringify({ project_id: projectId, expected_revision: expectedRevision, decision }),
  }),
  loadDocument: ({ documentId, projectId, fetchImpl } = {}) => request(`${API_PREFIX}/documents/${encodeURIComponent(documentId)}?project_id=${encodeURIComponent(projectId)}`, { fetchImpl }),
  saveDocument: ({ documentId, projectId, expectedRevision, markdown, fetchImpl } = {}) => request(`${API_PREFIX}/documents/${encodeURIComponent(documentId)}`, {
    fetchImpl,
    method: "PATCH",
    body: JSON.stringify({ project_id: projectId, expected_revision: expectedRevision, markdown }),
  }),
  retainTaskExperience: ({ taskId, projectId, fetchImpl } = {}) => request(`${API_PREFIX}/tasks/${encodeURIComponent(taskId)}/experience`, {
    fetchImpl,
    method: "POST",
    body: JSON.stringify({ project_id: projectId }),
  }),
  loadConstraints: ({ projectId, fetchImpl } = {}) => request(`${API_PREFIX}/constraints?project_id=${encodeURIComponent(projectId)}`, { fetchImpl }),
  saveConstraint: ({ projectId, constraintId, expectedRevision, content, enabled, validFrom, validUntil, fetchImpl } = {}) => request(`${API_PREFIX}/constraints/${encodeURIComponent(constraintId)}`, {
    fetchImpl, method: "PUT", body: JSON.stringify({ project_id: projectId, expected_revision: expectedRevision, content, enabled, valid_from: validFrom, valid_until: validUntil }),
  }),
  loadSettings: ({ fetchImpl } = {}) => request(`${API_PREFIX}/settings`, { fetchImpl }),
  saveGenerationMode: ({ mode, localEnabled, localBaseUrl, expectedRevision, fetchImpl } = {}) => request(`${API_PREFIX}/settings/generation-mode`, {
    fetchImpl,
    method: "PUT",
    body: JSON.stringify({ mode, local_enabled: localEnabled === true, local_base_url: localBaseUrl, expected_revision: expectedRevision }),
  }),
  saveSettings: ({ purpose, baseUrl, model, apiKey, allowRemote, enabled, expectedRevision, clearApiKey, fetchImpl } = {}) => request(`${API_PREFIX}/settings`, {
    fetchImpl,
    method: "PUT",
    body: JSON.stringify({ purpose, base_url: baseUrl, model, api_key: apiKey, allow_remote: allowRemote === true, enabled: enabled === true, expected_revision: expectedRevision, clear_api_key: clearApiKey === true }),
  }),
  testSettings: ({ purpose, fetchImpl } = {}) => request(`${API_PREFIX}/settings/test`, {
    fetchImpl,
    method: "POST",
    body: JSON.stringify({ purpose }),
  }),
});

export { API_PREFIX };
