import { settingsRequest } from './settingsApi';

export const providerStoreApi = {
  load: signal => settingsRequest('settings/provider-store', undefined, 'GET', signal),
  save: (enabled, value, signal) => settingsRequest('settings/provider-store', {
    enabled, expected_revision: value.revision,
    expected_generation_revision: value.generation_revision,
    expected_mode_revision: value.mode_revision,
  }, 'PUT', signal),
};
