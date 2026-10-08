import { productFetch } from '../../shared/api/deviceTransport';
import { readResponseJson } from '../../shared/lib/responseJson';
import { libraryBackendUrl } from '../rebuild/libraryOverviewTransport';

async function request(binding, options = {}) {
  const path = `/api/v2/workbench/turns/${encodeURIComponent(binding.turn_id)}/context-feedback`;
  const response = await productFetch(libraryBackendUrl(options.method ? path : `${path}?project_id=${encodeURIComponent(binding.project_id)}`),
    { cache: 'no-store', ...options });
  const value = await readResponseJson(response);
  if (!response.ok) throw new Error(value.detail || 'context_feedback_failed');
  return value;
}

export const contextFeedbackApi = {
  list: binding => request(binding),
  strike: (binding, entry) => request(binding, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project_id: binding.project_id, object_kind: 'recognition',
      object_id: entry.id, object_revision: entry.object_revision, expected_revision: 0 }) }),
};
