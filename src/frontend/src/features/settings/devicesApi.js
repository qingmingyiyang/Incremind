import { productFetch } from '../../shared/api/deviceTransport';
import { libraryBackendUrl } from '../rebuild/libraryOverviewTransport';
import { readResponseJson } from '../../shared/lib/responseJson';

async function request(path = '', body, method = body === undefined ? 'GET' : 'POST', expectedStatus) {
  const response = await productFetch(libraryBackendUrl('/api/v2/devices' + path), {
    method, cache: 'no-store',
    headers: { 'Content-Type': 'application/json' }, ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const value = await readResponseJson(response);
  if (!response.ok || expectedStatus !== undefined && response.status !== expectedStatus) {
    const error = new Error(response.status === 409 ? '设备已变化 · 刷新' : '配对未完成 · 重试');
    error.status = response.status;
    error.code = value?.detail;
    throw error;
  }
  return value;
}
export const devicesApi = {
  list: () => request(),
  pair: () => request('/pair', {}),
  exchange: (code, name) => request('/exchange', { code, name }),
  revoke: device => request('/' + encodeURIComponent(device.device_id) + '/revoke', { expected_revision: device.revision }),
  remove: device => request('/' + encodeURIComponent(device.device_id), { expected_revision: device.revision, confirmation: device.device_id }, 'DELETE', 200),
};
