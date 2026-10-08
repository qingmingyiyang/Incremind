import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { settingsApi } from '@src/features/settings/settingsApi';
let calls;
beforeEach(() => { calls = []; vi.stubGlobal('fetch', vi.fn(async (url, init = {}) => {
  calls.push({ url, method: init.method || 'GET', body: init.body ? JSON.parse(init.body) : undefined });
  const payload = String(url).endsWith('/cloud-asr-provider') ? { egress_manifest: { manifest_id: 'current-manifest' } } : String(url).endsWith('/subscriptions') ? { selection: { model: 'gpt-model', revision: 8 } } : {};
  return { ok: true, status: 200, json: async () => payload };
})); });
afterEach(() => vi.unstubAllGlobals());
it('revokes ASR consent using DELETE and grants only the newly read manifest after PUT', async () => {
  await settingsApi.disableAsr(); expect(calls[0]).toMatchObject({ method: 'DELETE', url: '/api/rebuild/settings/cloud-asr-provider/egress-consent' });
  calls.length = 0; await settingsApi.enableAsr(); expect(calls.map(row => row.method)).toEqual(['PUT', 'GET', 'POST']);
  expect(calls[0].body).toEqual({ enabled: true, confirm_enable: true }); expect(calls[2].body).toEqual({ manifest_id: 'current-manifest', confirm: true });
});
it('keeps subscription authorization on its actual selection and generation revisions', async () => {
  await settingsApi.toggleSubscription(false, 4); expect(calls[1]).toMatchObject({ method: 'PATCH', body: { model: 'gpt-model', expected_revision: 8, allow_remote: false, expected_generation_revision: 4 } });
});
it('clears the subscription selection before switching the separate generation mode', async () => {
  await settingsApi.saveMode({ mode: 'api', clearSubscription: true, localEnabled: true, localBaseUrl: 'http://127.0.0.1:8001/local-model/v1', expectedRevision: 2 });
  expect(calls.map(row => row.method)).toEqual(['GET', 'PATCH', 'PUT']); expect(calls[1].body).toEqual({ model: null, expected_revision: 8 }); expect(calls[2].body.expected_revision).toBe(2); expect(calls[2].body).not.toHaveProperty('clearSubscription');
});
it('does not send policy writes when the source revision cannot be read', async () => {
  fetch.mockResolvedValue({ ok: true, status: 200, json: async () => ({ experiences: [] }) });
  await expect(settingsApi.cancelPrivate({ source_id: 'missing', type: 'experience', project_id: 'alpha', policy_revision: 7 })).rejects.toThrow('source_revision_unavailable');
  expect(fetch).toHaveBeenCalledOnce(); expect(fetch.mock.calls[0][1]).not.toHaveProperty('body');
});

it('cancels privacy with the actual source and current policy revisions', async () => {
  fetch.mockImplementation(async (url, init = {}) => {
    calls.push({ url, body: init.body ? JSON.parse(init.body) : undefined });
    return { ok: true, status: 200, json: async () => String(url).includes('/workbench?') ? { experiences: [{ id: 'source-a', revision: 11 }] } : { nodes: [{ type: 'experience', id: 'source-a', policy_revision: 7 }] } };
  });
  await settingsApi.cancelPrivate({ source_id: 'source-a', type: 'experience', project_id: 'alpha', policy_revision: 7 });
  expect(calls[1].url).toContain('revision=11'); expect(calls[2].body).toEqual({ project_id: 'alpha', expected_source_revision: 11, expected_policy_revision: 7, allowed_purposes: ['generation', 'embedding', 'rerank'] });
});
it('uses the established desktop credential bridge for ASR without ordinary HTTP', async () => {
  const capture = vi.fn().mockResolvedValue({}); vi.stubGlobal('electronAPI', { captureCredential: capture });
  await settingsApi.saveAsrKey('sk-test-DO-NOT-LEAK');
  expect(capture).toHaveBeenCalledWith('tokenhub_asr_api_key', 'tokenhub-hy-asr', 'sk-test-DO-NOT-LEAK', expect.stringMatching(/^cmd-/)); expect(fetch).not.toHaveBeenCalled();
});
it('saves the complete external-agent preferences through their independent CAS endpoint', async () => {
  const preferences = { allow_remote: false, include_profile: true, daily_limit: 200, clients: { claude: true, codex: false }, expected_revision: 9 };
  await settingsApi.saveExternalAgent(preferences);
  expect(calls).toEqual([{ method: 'PATCH', url: '/api/v2/settings/external-agent', body: preferences }]);
});
