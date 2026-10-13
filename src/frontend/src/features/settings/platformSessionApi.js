import { settingsRequest } from './settingsApi';

const base = 'settings/platform-sessions';
export const platformSessionApi = {
  load: signal => settingsRequest(base, undefined, 'GET', signal),
  save: (platform, content, revision, signal) => settingsRequest(`${base}/${platform}`,
    { content, expected_revision: revision }, 'PUT', signal),
  remove: (platform, revision, signal) => settingsRequest(`${base}/${platform}`,
    { expected_revision: revision }, 'DELETE', signal),
  start: (platform, signal) => settingsRequest(`${base}/${platform}/login`, {}, 'POST', signal),
  poll: (id, signal) => settingsRequest(`${base}/login/${encodeURIComponent(id)}/poll`, {}, 'POST', signal),
  cancel: (id, signal) => settingsRequest(`${base}/login/${encodeURIComponent(id)}`, {}, 'DELETE', signal),
};
