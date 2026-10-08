import { productFetch as fetch } from '../../shared/api/deviceTransport';
import { libraryBackendUrl, responseJsonOrEmpty } from "./libraryOverviewTransport";

export const LIBRARY_OVERVIEW_ENDPOINT = "/api/rebuild/library/overview";
export const LIBRARY_ACTIVITY_OVERVIEW_ENDPOINT = "/api/rebuild/library/activity-overview";
export const LIBRARY_SEARCH_ENDPOINT = "/api/rebuild/library/search";
export const INSPIRATION_OVERVIEW_ENDPOINT = "/api/rebuild/inspirations/overview";
export const DAILY_REMINDERS_ENDPOINT = "/api/rebuild/reminders/today";

export function libraryOverviewUrl({ projectId } = {}) {
  const endpoint = libraryBackendUrl(LIBRARY_OVERVIEW_ENDPOINT);
  if (!projectId) {
    return endpoint;
  }
  const separator = endpoint.includes("?") ? "&" : "?";
  return `${endpoint}${separator}project_id=${encodeURIComponent(projectId)}`;
}

export function libraryActivityOverviewUrl({ projectId, year } = {}) {
  const endpoint = libraryBackendUrl(LIBRARY_ACTIVITY_OVERVIEW_ENDPOINT);
  const params = new URLSearchParams();
  if (projectId) {
    params.set("project_id", projectId);
  }
  if (Number.isInteger(year) && year > 0) {
    params.set("year", String(year));
  }
  const query = params.toString();
  return query ? `${endpoint}?${query}` : endpoint;
}

export function inspirationOverviewUrl({ projectId } = {}) {
  const endpoint = libraryBackendUrl(INSPIRATION_OVERVIEW_ENDPOINT);
  if (!projectId) {
    return endpoint;
  }
  const separator = endpoint.includes("?") ? "&" : "?";
  return `${endpoint}${separator}project_id=${encodeURIComponent(projectId)}`;
}

export function dailyRemindersUrl({ projectId, limit } = {}) {
  const endpoint = libraryBackendUrl(DAILY_REMINDERS_ENDPOINT);
  const params = new URLSearchParams();
  if (projectId) {
    params.set("project_id", projectId);
  }
  if (Number.isInteger(limit) && limit > 0) {
    params.set("limit", String(limit));
  }
  const query = params.toString();
  return query ? `${endpoint}?${query}` : endpoint;
}

// 面向普通资料库的稳定文案。内部 reason 仅保留给调用方诊断，绝不直接显示，
// 因为它可能包含 authority、provider 或 SecretLease 的实现细节。
export function libraryOverviewSafeErrorMessage(status, payload = {}) {
  const detail = typeof payload?.detail === "string" ? payload.detail : "";
  if (status === 400) return "当前资料库请求无效，请检查项目后重试。";
  if (status === 403) return "本地桌面会话已失效，请重新打开应用后重试。";
  if (status === 409) return "资料库状态已变化或正在恢复，请刷新后重试。";
  if (status >= 500 || status === 0) return "暂时无法读取资料库，请稍后重试。";
  if (/secret|lease|credential|provider/i.test(detail)) {
    return "模型连接暂时不可用，请检查连接设置后重试。";
  }
  return "暂时无法读取资料库，请稍后重试。";
}

export async function loadLibraryOverview({ projectId, fetchImpl = fetch } = {}) {
  const response = await fetchImpl(libraryOverviewUrl({ projectId }), {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    const payload = typeof response.json === "function"
      ? await responseJsonOrEmpty(response)
      : {};
    const error = new Error(libraryOverviewSafeErrorMessage(response.status, payload));
    error.status = response.status;
    error.code = typeof payload?.reason === "string" ? payload.reason : "";
    throw error;
  }
  return response.json();
}

export async function loadLibraryActivityOverview({ projectId, year, fetchImpl = fetch } = {}) {
  const response = await fetchImpl(libraryActivityOverviewUrl({ projectId, year }), {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`Library Activity Overview failed with ${response.status}`);
  }
  return response.json();
}

export async function loadInspirationOverview({ projectId, fetchImpl = fetch } = {}) {
  const response = await fetchImpl(inspirationOverviewUrl({ projectId }), {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`Inspiration Overview failed with ${response.status}`);
  }
  return response.json();
}

export async function loadDailyReminders({ projectId, limit, fetchImpl = fetch } = {}) {
  const response = await fetchImpl(dailyRemindersUrl({ projectId, limit }), {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`Daily reminders failed with ${response.status}`);
  }
  return response.json();
}

// Phase 5 子项1：搜索优先 active FTS5。
// 后端 GET /api/rebuild/library/search?q=...&limit=...&project_id=...&layers=...&trust=...
// 返回 { status, backend, query, total, index_stale, reason, hits: [...] }。
export function librarySearchUrl({
  query,
  projectId,
  layers,
  trust,
  limit,
  scope,
  filterId,
  tag,
  importBatchId,
  offset,
} = {}) {
  const endpoint = libraryBackendUrl(LIBRARY_SEARCH_ENDPOINT);
  const params = new URLSearchParams();
  const trimmed = String(query || "").trim();
  if (trimmed) {
    params.set("q", trimmed);
  }
  if (projectId) {
    params.set("project_id", projectId);
  }
  if (Array.isArray(layers) && layers.length > 0) {
    params.set("layers", layers.filter(Boolean).join(","));
  }
  if (Array.isArray(trust) && trust.length > 0) {
    params.set("trust", trust.filter(Boolean).join(","));
  }
  if (Number.isInteger(limit) && limit > 0) {
    params.set("limit", String(limit));
  }
  if (scope === "overview") {
    params.set("scope", scope);
    if (filterId) params.set("filter_id", filterId);
    if (tag) params.set("tag", tag);
    if (importBatchId) params.set("import_batch_id", importBatchId);
    if (Number.isInteger(offset) && offset >= 0) params.set("offset", String(offset));
  }
  const queryString = params.toString();
  return queryString ? `${endpoint}?${queryString}` : endpoint;
}

export async function searchLibrary({
  query,
  projectId,
  layers,
  trust,
  limit,
  scope,
  filterId,
  tag,
  importBatchId,
  offset,
  fetchImpl = fetch,
} = {}) {
  const response = await fetchImpl(
    librarySearchUrl({ query, projectId, layers, trust, limit, scope, filterId, tag, importBatchId, offset }),
    {
      headers: { Accept: "application/json" },
    },
  );
  if (!response.ok) {
    throw new Error(`Library search failed with ${response.status}`);
  }
  return response.json();
}
