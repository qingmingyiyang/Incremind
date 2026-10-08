import { productFetch } from '../../shared/api/deviceTransport';
import { libraryBackendUrl } from '../rebuild/libraryOverviewTransport';
import { readResponseJson } from '../../shared/lib/responseJson';

async function request(path = '', body) {
  const response = await productFetch(libraryBackendUrl('/api/v2/devices' + path), {
    method: body === undefined ? 'GET' : 'POST', cache: 'no-store',
    headers: { 'Content-Type': 'application/json' }, ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const value = await readResponseJson(response);
  if (!response.ok) {
    const error = new Error(response.status === 409 ? '设备已变化 · 刷新' : '配对未完成 · 重试');
    error.status = response.status;
    throw error;
  }
  return value;
}
export const devicesApi = {
  list: () => request(),
  pair: () => request('/pair', {}),
  exchange: (code, name) => request('/exchange', { code, name }),
  revoke: device => request('/' + encodeURIComponent(device.device_id) + '/revoke', { expected_revision: device.revision }),
};
