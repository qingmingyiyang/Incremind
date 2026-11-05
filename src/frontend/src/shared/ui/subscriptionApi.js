import { productFetch as fetch } from '../api/deviceTransport';
import { assertServerAvailable, readResponseJson } from '../lib/responseJson';
import { libraryBackendUrl } from '../../features/rebuild/libraryOverviewTransport';

export function subscriptionMessage(code, status) {
  const messages = {
    subscription_callback_invalid: '回调地址不匹配 · 复制本次登录后的完整地址',
    subscription_callback_too_large: '回调地址过长 · 重新复制',
    subscription_callback_consumed: '授权已提交 · 正在读取状态',
    subscription_login_expired: '登录已过期 · 重新登录',
    subscription_login_not_found: '本次登录已结束 · 重新登录',
    subscription_login_in_progress: '已有登录等待完成 · 稍后重试',
    subscription_login_denied: '未授予登录权限 · 重新登录',
    subscription_identity_unavailable: '登录身份已变化 · 重新登录',
    subscription_configuration_changed: '设置已变化 · 重新登录',
    subscription_registration_invalid: '授权不完整 · 重新登录',
  };
  return messages[code] || (status === 409 ? '设置已变化 · 刷新' : status === 401 ? '订阅已过期 · 登录' : '连接未完成 · 重试');
}

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
    const error = new Error(subscriptionMessage(value?.detail, response.status));
    error.status = response.status;
    throw error;
  }
  return value;
}
