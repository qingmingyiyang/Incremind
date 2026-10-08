import { productFetch } from '../../shared/api/deviceTransport';
import { libraryBackendUrl } from '../rebuild/libraryOverviewTransport';
import { readResponseJson } from '../../shared/lib/responseJson';

async function request(path = '', method = 'GET', body) {
  const response = await productFetch(libraryBackendUrl('/api/v2/server' + path), {
    method, cache: 'no-store', headers: { 'Content-Type': 'application/json' },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const value = await readResponseJson(response);
  if (!response.ok) {
    const error = new Error('user_request_failed'); error.status = response.status; throw error;
  }
  return value;
}

export const usersApi = {
  list: () => request('/users'),
  audit: () => request('/audit'),
  create: name => request('/users', 'POST', { name }),
  update: (user, changes) => request('/users/' + encodeURIComponent(user.user_id), 'PATCH', { expected_revision: user.revision, ...changes }),
  pair: user => request('/users/' + encodeURIComponent(user.user_id) + '/pair', 'POST', {}),
};
