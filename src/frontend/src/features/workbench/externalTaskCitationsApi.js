import { productFetch } from '../../shared/api/deviceTransport';
import { readResponseJson } from '../../shared/lib/responseJson';
import { libraryBackendUrl } from '../rebuild/libraryOverviewTransport';

const identity = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/.test(value);
const automaticIds = value => Array.isArray(value) && value.every(item => typeof item === 'string' && /^M[1-9][0-9]*$/.test(item))
  && new Set(value).size === value.length;
const registeredIds = value => Array.isArray(value) && value.every(item => typeof item === 'string' && /^[MP][1-9][0-9]*$/.test(item))
  && new Set(value).size === value.length;

export function readTaskCitations(value, binding = value) {
  if (!value || !binding || !identity(value.product_id) || !identity(value.project_id)
      || value.product_id !== binding.product_id || value.project_id !== binding.project_id
      || !['recorded', 'pending', 'unknown', 'not_qualified'].includes(value.state)
      || (value.state === 'unknown' ? value.registered_ids !== null || value.ids !== null && !automaticIds(value.ids)
        : !registeredIds(value.registered_ids) || (value.state === 'not_qualified' ? value.ids !== null : !automaticIds(value.ids)
          || (value.state === 'recorded') !== value.ids.every(item => value.registered_ids.includes(item))))) {
    throw new Error('external_task_citations_invalid');
  }
  return value;
}

async function request(binding, { retry = false, signal } = {}) {
  if (!identity(binding?.product_id) || !identity(binding?.project_id)) throw new Error('external_task_citations_invalid');
  const path = `/api/v2/external-agent/tasks/${encodeURIComponent(binding.product_id)}/citations`;
  const response = await productFetch(libraryBackendUrl(retry ? path : `${path}?project_id=${encodeURIComponent(binding.project_id)}`),
    { cache: 'no-store', signal, ...(retry ? { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_id: binding.project_id }) } : {}) });
  const value = await readResponseJson(response);
  if (!response.ok) throw new Error('external_task_citations_unavailable');
  return readTaskCitations(value, binding);
}

export const externalTaskCitationsApi = { read: (binding, options) => request(binding, options),
  retry: (binding, options) => request(binding, { ...options, retry: true }) };
