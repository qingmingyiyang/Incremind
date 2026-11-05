import { settingsRequest } from './settingsApi';

const userPath = user => `server/users/${encodeURIComponent(user.user_id)}`;
export const serverModelsApi = {
  profiles: () => settingsRequest('server/model-profiles'),
  saveProfile: (profile, body) => settingsRequest(`server/model-profiles${profile ? '/' + encodeURIComponent(profile.profile_id) : ''}`,
    { ...body, ...(profile ? { expected_revision: profile.revision } : {}) }, profile ? 'PUT' : 'POST'),
  meteringPrices: profile => settingsRequest(`server/model-profiles/${encodeURIComponent(profile.profile_id)}/metering-prices`),
  saveMeteringPrices: (profile, baseline, models) => settingsRequest(`server/model-profiles/${encodeURIComponent(profile.profile_id)}/metering-prices`, {
    models, expected_revision: baseline.revision, expected_profile_revision: baseline.profile_revision,
  }, 'PUT'),
  allocation: user => settingsRequest(`${userPath(user)}/model-allocation`),
  saveAllocation: (user, current, body) => settingsRequest(`${userPath(user)}/model-allocation`, {
    ...body, expected_revision: current.revision, expected_user_revision: current.user_revision,
  }, 'PUT'),
  balance: user => settingsRequest(`${userPath(user)}/model-balance`),
  attempts: (user, state = 'all', cursor) => settingsRequest(`${userPath(user)}/model-balance/attempts?${new URLSearchParams({ state, limit: '50', ...(cursor ? { cursor } : {}) })}`),
  adjust: (user, balance, amount, reason) => settingsRequest(`${userPath(user)}/model-balance/adjust`, {
    amount, reason, expected_revision: balance.revision, expected_user_revision: user.revision,
  }, 'POST'),
  reconcile: (user, attempt, amount, reason) => settingsRequest(`${userPath(user)}/model-balance/attempts/${encodeURIComponent(attempt.attempt_id)}/reconcile`, {
    amount, reason, expected_revision: attempt.revision, expected_user_revision: user.revision,
  }, 'POST'),
};

export const modelPurposes = { generation: '生成', embedding: '向量', rerank: '重排', vision: '识图', search: '搜索', asr: '转写' };
