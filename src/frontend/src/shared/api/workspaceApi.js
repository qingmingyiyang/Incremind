import { productFetch as fetch } from './deviceTransport';
const ROOT = "/api/workspace/v1";

function endpoint(path) {
  const base = globalThis.electronAPI?.backendBaseUrl || ("");
  return `${base.replace(/\/$/, "")}${ROOT}${path}`;
}

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(endpoint(path), options);
  } catch (error) {
    throw new Error(`无法连接本地服务：${error.message}`);
  }
  const raw = await response.text();
  let body;
  try { body = raw ? JSON.parse(raw) : null; } catch { body = raw; }
  if (!response.ok) {
    const detail = typeof body === "object" && body ? body.detail || body.message || body.error : body;
    const error = new Error(`${detail ? (typeof detail === "string" ? detail : JSON.stringify(detail)) : response.statusText || "请求失败"} (${response.status})`);
    error.status = response.status;
    if (detail && typeof detail === "object") {
      error.code = detail.code;
      error.current = detail.current;
    }
    throw error;
  }
  return body;
}

const json = (value) => ({ headers: { "Content-Type": "application/json" }, body: JSON.stringify(value) });
const revision = (value) => {
  if (!Number.isSafeInteger(value) || value < 1) throw new Error("草稿版本不可用，请刷新材料后重试");
  return value;
};
const documentBasis = (value) => {
  if (value === null) return null;
  if (!value || typeof value.id !== "string" || !value.id.trim()) throw new Error("文档基线不可用，请刷新材料后重试");
  return { id: value.id, revision: revision(value.revision) };
};
const expectedMarkdown = (value) => {
  if (typeof value !== "string") throw new Error("确认正文不可用，请核对草稿后重试");
  return value;
};
const itemPath = (id) => `/items/${encodeURIComponent(id)}`;
const legacyReviewPath = (sourceId) => `/legacy-reviews/${encodeURIComponent(sourceId)}`;

export const workspaceApi = {
  list: (projectId) => request(`/items?${new URLSearchParams({ project_id: projectId })}`),
  legacyReviews: (projectId) => request(`/legacy-reviews?${new URLSearchParams({ project_id: projectId })}`),
  saveLegacyReviewDraft: (projectId, sourceId, markdown, expectedRevision, expectedDocumentBasis) => request(`${legacyReviewPath(sourceId)}/draft`, {
    method: "PUT", ...json({ project_id: projectId, markdown, expected_revision: revision(expectedRevision), expected_document_basis: documentBasis(expectedDocumentBasis) }),
  }),
  confirmLegacyReview: (projectId, sourceId, expectedRevision, expectedDocumentBasis, markdown) => request(`${legacyReviewPath(sourceId)}/confirm`, {
    method: "POST", ...json({ project_id: projectId, expected_revision: revision(expectedRevision), expected_document_basis: documentBasis(expectedDocumentBasis), expected_markdown: expectedMarkdown(markdown) }),
  }),
  legacyRecognition: (projectId, sourceId) => request(`${legacyReviewPath(sourceId)}/recognition`, {
    method: "POST", ...json({ project_id: projectId }),
  }),
  text: (projectId, text, title) => request("/items/text", { method: "POST", ...json({ project_id: projectId, text, ...(title ? { title } : {}) }) }),
  link: (projectId, url) => request("/items/link", { method: "POST", ...json({ project_id: projectId, url }) }),
  file: (projectId, file) => {
    const form = new FormData();
    form.append("project_id", projectId);
    form.append("file", file);
    return request("/items/file", { method: "POST", body: form });
  },
  process: (projectId, id, remoteProcessingConsent = false) => request(`${itemPath(id)}/process`, {
    method: "POST", ...json({ project_id: projectId, remote_processing_consent: remoteProcessingConsent === true }),
  }),
  processingTargets: async () => {
    const base = globalThis.electronAPI?.backendBaseUrl || ("");
    const [generationResponse, asrResponse] = await Promise.all([
      fetch(`${String(base).replace(/\/$/, "")}/api/recognition/settings`, { cache: "no-store" }),
      fetch(`${String(base).replace(/\/$/, "")}/api/rebuild/settings/cloud-asr-provider`, { cache: "no-store" }),
    ]);
    if (!generationResponse.ok || !asrResponse.ok) throw new Error("处理服务设置暂不可读取");
    const [generationSettings, asrSettings] = await Promise.all([generationResponse.json(), asrResponse.json()]);
    const generation = generationSettings.generation || {};
    const host = generation.base_url ? new URL(generation.base_url).hostname : "";
    const remoteGeneration = Boolean(host && !["localhost", "127.0.0.1", "::1"].includes(host));
    return {
      remote: remoteGeneration || asrSettings.enabled === true,
      generation: remoteGeneration ? { host, baseUrl: generation.base_url, model: generation.model, revision: generation.revision } : null,
      asr: asrSettings.enabled === true ? { host: new URL(asrSettings.endpoint).hostname, endpoint: asrSettings.endpoint, model: asrSettings.model, revision: asrSettings.settings_revision } : null,
    };
  },
  saveDraft: (projectId, id, draft, expectedRevision) => request(`${itemPath(id)}/draft`, { method: "PUT", ...json({ project_id: projectId, ...draft, expected_revision: revision(expectedRevision) }) }),
  confirm: (projectId, id, expectedRevision) => request(`${itemPath(id)}/confirm`, { method: "POST", ...json({ project_id: projectId, expected_revision: revision(expectedRevision) }) }),
  source: (projectId, id, options = {}) => request(`${itemPath(id)}/source?${new URLSearchParams({ project_id: projectId })}`, options),
  search: (projectId, q) => request(`/search?${new URLSearchParams({ project_id: projectId, q })}`),
  askPreview: (projectId, question) => request("/ask/preview", { method: "POST", ...json({ project_id: projectId, question }) }),
  ask: (projectId, question, previewId, remoteProcessingConsent = false) => request("/ask", {
    method: "POST", ...json({ project_id: projectId, question, preview_id: previewId, remote_processing_consent: remoteProcessingConsent === true }),
  }),
  recognition: (projectId, id) => request(`${itemPath(id)}/recognition`, { method: "POST", ...json({ project_id: projectId }) }),
  retry: (projectId, id) => request(`${itemPath(id)}/retry`, { method: "POST", ...json({ project_id: projectId }) }),
};
