import { productFetch as fetch } from '../api/deviceTransport';
import { assertServerAvailable, readResponseJson } from '../lib/responseJson';
import { libraryBackendUrl } from '../../features/rebuild/libraryOverviewTransport';

export async function subscriptionRequest(path = '', body, method = 'GET', signal) {
  let response;
  try {
    response = await fetch(libraryBackendUrl(`/api/v2/settings/subscriptions${path}`), {
      method, signal, cache: 'no-store', headers: { 'Content-Type': 'application/json' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  } catch (error) {
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    throw new Error('连接未完成 · 重试');
  }
  assertServerAvailable(response);
  const value = await readResponseJson(response);
  if (!response.ok) {
    const error = new Error(response.status === 409 ? '设置已变化 · 刷新' : response.status === 401 ? '订阅已过期 · 登录' : '连接未完成 · 重试');
    throw error;
  }
  return value;
}
