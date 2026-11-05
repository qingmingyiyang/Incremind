import { productFetch } from '../../shared/api/deviceTransport';
import { libraryBackendUrl } from '../rebuild/libraryOverviewTransport';
import { readResponseJson } from '../../shared/lib/responseJson';

async function request(path = '', method = 'GET', body, expectedStatus) {
  const response = await productFetch(libraryBackendUrl('/api/v2/server' + path), {
    method, cache: 'no-store', headers: { 'Content-Type': 'application/json' },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const value = await readResponseJson(response);
  if (!response.ok || expectedStatus !== undefined && response.status !== expectedStatus) {
    const error = new Error('user_request_failed'); error.status = response.status; error.code = value?.detail; throw error;
  }
  return value;
}

export const usersApi = {
  list: () => request('/users'),
  audit: () => request('/audit'),
  auditObjects: auditId => request('/audit/' + encodeURIComponent(auditId) + '/objects'),
  create: (name, login_name, password) => request('/users', 'POST', { name, ...(login_name === undefined ? {} : { login_name, password }) }),
  login: (user, login_name, password) => request('/users/' + encodeURIComponent(user.user_id) + '/login', 'PUT', {
    login_name, password, expected_revision: user.revision, expected_login_revision: user.login_revision ?? 0,
  }),
  update: (user, changes) => request('/users/' + encodeURIComponent(user.user_id), 'PATCH', { expected_revision: user.revision, ...changes }),
  pair: user => request('/users/' + encodeURIComponent(user.user_id) + '/pair', 'POST', {}),
  remove: user => request('/users/' + encodeURIComponent(user.user_id), 'DELETE', { expected_revision: user.revision, confirmation: user.user_id }, 200),
};
