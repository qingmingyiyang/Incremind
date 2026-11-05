import { settingsRequest } from './settingsApi';

export const providerConnectionsApi = {
  list: signal => settingsRequest('settings/provider-connections', undefined, 'GET', signal),
  save: (id, body) => settingsRequest(`settings/provider-connections/${encodeURIComponent(id)}`, body, 'PUT'),
  disconnect: (id, body) => settingsRequest(`settings/provider-connections/${encodeURIComponent(id)}/disconnect`, body, 'POST'),
  select: (id, body) => settingsRequest(`settings/provider-connections/${encodeURIComponent(id)}/selection`, body, 'POST'),
  operation: id => settingsRequest(`settings/provider-connections/operations/${encodeURIComponent(id)}`),
  cancelOperation: id => settingsRequest(`settings/provider-connections/operations/${encodeURIComponent(id)}/cancel`, {}, 'POST'),
  trial: body => settingsRequest('settings/model-trials', body, 'POST'),
  trialStatus: (id, signal) => settingsRequest(`settings/model-trials/${encodeURIComponent(id)}`, undefined, 'GET', signal),
  cancelTrial: id => settingsRequest(`settings/model-trials/${encodeURIComponent(id)}/cancel`, {}, 'POST'),
};
