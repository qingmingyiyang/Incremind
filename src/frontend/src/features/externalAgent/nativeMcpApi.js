import { productFetch } from '../../shared/api/deviceTransport';
import { libraryBackendUrl } from '../rebuild/libraryOverviewTransport';
import { readResponseJson } from '../../shared/lib/responseJson';

async function request(path, { method = 'GET', body, signal } = {}) {
  const response = await productFetch(libraryBackendUrl(`/api/v2/native-mcp${path}`), {
    method, signal, cache: 'no-store', headers: { Accept: 'application/json', ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const data = await readResponseJson(response);
  if (!response.ok) {
    const error = new Error(response.status === 401 ? '请先登录主应用' : response.status === 409 ? '状态已变化 · 重新读取后操作' : response.status === 403 ? '当前账号没有此权限' : '请求未完成 · 重新读取');
    error.status = response.status; error.code = typeof data?.detail === 'string' ? data.detail : null;
    if (error.code === 'native_mcp_configuration_invalid') error.message = '填写完整 HTTPS 地址，地址中不含路径';
    throw error;
  }
  return data;
}

export const nativeMcpApi = {
  read: signal => request('', { signal }),
  configure: (body, signal) => request('/configuration', { method: 'PUT', body, signal }),
  preference: (body, signal) => request('/chatgpt', { method: 'PUT', body, signal }),
  issue: (body, signal) => request('/keys', { method: 'POST', body, signal }),
  revoke: (id, body, signal) => request(`/keys/${encodeURIComponent(id)}/revoke`, { method: 'POST', body, signal }),
  register: (body, signal) => request('/oauth-clients', { method: 'POST', body, signal }),
  consent: (id, signal) => request(`/consent/${encodeURIComponent(id)}`, { signal }),
  decide: (id, body, signal) => request(`/consent/${encodeURIComponent(id)}`, { method: 'POST', body, signal }),
  candidate: (id, project, signal) => request(`/review/${encodeURIComponent(id)}?project_id=${encodeURIComponent(project)}`, { signal }),
  prepare: (id, body, signal) => request(`/review/${encodeURIComponent(id)}/prepare`, { method: 'POST', body, signal }),
  commit: (id, body, signal) => request(`/review/${encodeURIComponent(id)}/commit`, { method: 'POST', body, signal }),
};

export function nativeClientConfiguration(origin, client) {
  const url = new URL(origin);
  if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash || url.pathname !== '/') throw new Error('native_mcp_https_required');
  if (client === 'codex') return `codex mcp add chriptmas-memory --url '${`${url.origin}/mcp`.replaceAll("'", "''")}' --bearer-token-env-var CHRIPTMAS_MCP_DEVICE_KEY`;
  if (client === 'claude') return JSON.stringify({ mcpServers: { 'chriptmas-memory': {
    type: 'http', url: `${url.origin}/mcp`, headers: { Authorization: 'Bearer ${CHRIPTMAS_MCP_DEVICE_KEY}' },
  } } }, null, 2);
  throw new Error('native_mcp_client_invalid');
}
