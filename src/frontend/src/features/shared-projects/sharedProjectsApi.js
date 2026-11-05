import { productFetch } from '../../shared/api/deviceTransport';
import { readResponseJson } from '../../shared/lib/responseJson';
import { libraryBackendUrl } from '../rebuild/libraryOverviewTransport';
import { sharedProjectList, sharedProjectDetail, sharedDirectory, sharedInvitations, sharedActivity, sharedPending } from './sharedProjectViews';
import { sharedScopes } from './sharedScopeViews';

const base = '/api/v2/server/shared-projects';
const part = value => encodeURIComponent(value);
async function request(path, { body, signal, idempotencyKey } = {}) {
  const response = await productFetch(libraryBackendUrl(base + path), {
    method: body === undefined ? 'GET' : 'POST', cache: 'no-store', signal,
    headers: { ...(body === undefined ? {} : { 'Content-Type': 'application/json' }), ...(idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : {}) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const value = await readResponseJson(response);
  if (!response.ok) {
    const originalUnavailable = response.status === 409 && value?.detail === 'shared_extraction_original_unavailable';
    const error = new Error(originalUnavailable ? '原件不可用 · 核对来源'
      : response.status === 409 ? '资格或内容已变化，请重新读取'
      : response.status === 403 ? '当前没有操作权限' : response.status === 401 ? '请重新登录' : '操作未完成，请重试');
    error.status = response.status; error.code = value?.detail; error.operationId = value?.operation_id;
    throw error;
  }
  return value;
}
export const sharedProjectsApi = {
  scopes: signal => request('/scopes', { signal }).then(sharedScopes),
  extractionPreview: (project, body, key, signal) => request(`/${part(project)}/extractions`, { body, idempotencyKey: key, signal }),
  extractionCommand: (key, signal) => request(`/extraction-commands/${part(key)}`, { signal }),
  extraction: (id, signal) => request(`/extractions/${part(id)}`, { signal }),
  extractionProgress: (id, signal) => request(`/extractions/${part(id)}/progress`, { signal }),
  adoptExtraction: (id, body) => request(`/extractions/${part(id)}/adopt`, { body }),
  reconcileExtraction: id => request(`/extractions/${part(id)}/reconcile`, { body: {} }),
  extractedInsight: (id, signal) => request(`/extracted-insights/${part(id)}`, { signal }),
  operation: operationId => request(`/operations/${part(operationId)}`),
  cancelUnreceived: command => request(`/operations/${part(command.id)}/cancel-unreceived`, {
    body: { action: command.expected.action, project_id: command.projectId, inputs: command.expected.inputs },
  }),
  sendCommand: command => request(command.path, { body: command.body }),
  list: options => request('', options).then(sharedProjectList),
  users: options => request('/users', options).then(sharedDirectory),
  invitations: options => request('/invitations', options).then(sharedInvitations),
  detail: (id, options) => request(`/${part(id)}`, options).then(data => sharedProjectDetail(data, id)),
  activity: (id, userId, options) => request(`/${part(id)}/activity${userId ? `?${new URLSearchParams({ actor_user_id: userId })}` : ''}`, options).then(sharedActivity),
  pending: (id, options) => request(`/${part(id)}/operations`, options).then(rows => sharedPending(rows, id)),
  create: (projectName, operationId) => request('', { body: { project_name: projectName, operation_id: operationId } }),
  invite: (project, userId, role, operationId) => request(`/${part(project.id)}/invitations`, {
    body: { user_id: userId, role, authority: project.authority, operation_id: operationId },
  }),
  accept: (invitation, operationId) => request(`/invitations/${part(invitation.id)}/accept`, {
    body: { expected_revision: invitation.revision, operation_id: operationId },
  }),
  member: (project, member, action, role, operationId) => request(`/${part(project.id)}/members/${part(member.user_id)}`, {
    body: { action, role, expected_revision: member.revision, authority: project.authority, operation_id: operationId },
  }),
  resume: operationId => request(`/operations/${part(operationId)}/resume`, { body: {} }),
  delete: (project, confirmation, operationId) => request(`/${part(project.id)}/delete`, {
    body: { confirmation, authority: project.authority, operation_id: operationId, skip_backup: true },
  }),
};
