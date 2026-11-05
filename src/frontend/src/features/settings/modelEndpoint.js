export const DEFAULT_LOCAL_MODEL_BASE_URL = 'http://127.0.0.1:8001/local-model/v1';
const loopbackHosts = new Set(['localhost', '127.0.0.1', '[::1]']);

export function apiEndpointError(value) {
  if (!value.trim()) return '';
  let endpoint;
  try { endpoint = new URL(value.trim()); }
  catch { return '填写完整的 API 地址'; }
  if (loopbackHosts.has(endpoint.hostname.toLowerCase())) return '本机地址使用生成用途的本机方式';
  if (endpoint.protocol !== 'https:') return '外接 API 地址使用 HTTPS';
  return '';
}

export function isLocalModelEndpoint(value) {
  if (typeof value !== 'string' || value.length > 2048) return false;
  const clean = value.trim().replace(/\/+$/, '');
  try {
    const endpoint = new URL(clean);
    if (endpoint.protocol !== 'http:' || !loopbackHosts.has(endpoint.hostname.toLowerCase())
        || endpoint.pathname !== '/local-model/v1' || endpoint.username || endpoint.password
        || endpoint.search || endpoint.hash) return false;
    // URL 已验证结构与端口；固定合同检查原字面形状，同时保留 :80 与 :080。
    return /^http:\/\/(?:@|:@)?(?:localhost|127\.0\.0\.1|\[::1\]):[0-9]+\/local-model\/v1(?:\?)?(?:#)?$/i.test(clean);
  } catch { return false; }
}

export function localBaseUrlForMode(next, generationMode) {
  if (next === 'api' && generationMode.revision === 0 && !isLocalModelEndpoint(generationMode.local_base_url)) return DEFAULT_LOCAL_MODEL_BASE_URL;
  return generationMode.local_base_url;
}
