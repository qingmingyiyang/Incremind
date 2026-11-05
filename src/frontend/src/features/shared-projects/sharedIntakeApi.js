import { productFetch } from '../../shared/api/deviceTransport';
import { readResponseJson } from '../../shared/lib/responseJson';
import { libraryBackendUrl } from '../rebuild/libraryOverviewTransport';
import { sharedInspirationResult } from './sharedInspirationCommands';

async function request(command, send, signal) {
  const path = send ? command.path : `/${encodeURIComponent(command.projectId)}/intake/commands/${encodeURIComponent(command.id)}`;
  const response = await productFetch(libraryBackendUrl(`/api/v2/server/shared-projects${path}`), {
    method: send ? 'POST' : 'GET', cache: 'no-store', signal,
    ...(send ? { headers: { 'Content-Type': 'application/json', 'Idempotency-Key': command.id }, body: JSON.stringify(command.body) } : {}),
  });
  const value = await readResponseJson(response);
  if (!response.ok) {
    const error = new Error(response.status === 403 ? '当前没有操作权限' : response.status === 409 ? '资格或内容已变化 · 核对'
      : response.status === 429 ? '空间额度不足 · 联系管理员' : '结果待核对');
    error.status = response.status; error.code = value?.detail; throw error;
  }
  return sharedInspirationResult(value, command);
}
export const sharedIntakeApi = {
  read: (command, signal) => request(command, false, signal),
  send: (command, signal) => request(command, true, signal),
};
