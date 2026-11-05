const identity = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/.test(value);
const positive = value => Number.isSafeInteger(value) && value > 0;
const integer = value => Number.isSafeInteger(value) && value >= 0;
const states = new Set(['preview', 'pending', 'completed']);
const unique = values => new Set(values).size === values.length;
const invalid = () => { throw new Error('提取记录未核对，请重新读取'); };

export function extractionCommandResult(value, expectedId) {
  if (value?.command_id !== expectedId || !['received', 'unreceived'].includes(value.state)
    || value.state === 'received' && !identity(value.preview_id)) invalid();
  return value;
}

export function extractionPreview(value, expected = {}) {
  if (!identity(value?.id) || !positive(value.revision) || !states.has(value.state)
    || !identity(value.shared_project_id) || !identity(value.target_project_id)
    || expected.id && value.id !== expected.id || expected.source && value.shared_project_id !== expected.source
    || expected.target && value.target_project_id !== expected.target
    || !Array.isArray(value.recognition_ids) || !value.recognition_ids.length || !unique(value.recognition_ids)
    || value.recognition_ids.some(id => !identity(id)) || !Array.isArray(value.items)
    || JSON.stringify(value.items.map(item => item?.id)) !== JSON.stringify(value.recognition_ids)
    || expected.ids && JSON.stringify([...value.recognition_ids].sort()) !== JSON.stringify([...expected.ids].sort())
    || value.items.some(item => !positive(item?.revision) || typeof item.text !== 'string'
      || !Array.isArray(item.conditions) || item.conditions.some(text => typeof text !== 'string'))
    || !Array.isArray(value.materials) || !unique(value.materials.map(item => item?.id))
    || value.materials.some(item => !identity(item?.id) || typeof item.title !== 'string' || typeof item.text !== 'string'
      || typeof item.private !== 'boolean' || !Array.isArray(item.file_names) || item.file_names.some(name => typeof name !== 'string'))) invalid();
  return value;
}

export function extractionProgress(value, expectedId, expectedTarget) {
  if (!identity(value?.id) || value.id !== expectedId || !states.has(value.state)
    || !states.has(value.personal_state) || !['pending', 'completed'].includes(value.activity_state)
    || !positive(value.total) || !integer(value.completed) || !integer(value.remaining)
    || value.completed + value.remaining !== value.total || !Array.isArray(value.copies)
    || value.copies.length !== value.completed || !unique(value.copies.map(item => item?.id))
    || value.copies.some(item => !identity(item?.id) || !identity(item.target_project_id) || !identity(item.target_recognition_id)
      || expectedTarget && item.target_project_id !== expectedTarget)
    || value.personal_state === 'completed' && value.completed !== value.total
    || value.state === 'completed' && (value.remaining !== 0 || value.personal_state !== 'completed' || value.activity_state !== 'completed')) invalid();
  return value;
}

function personalFile(file, item, project) {
  if (typeof file?.name !== 'string' || !integer(file.size) || typeof file.url !== 'string' || !file.url.startsWith('/api/')) return false;
  const url = new URL(file.url, 'http://localhost');
  return url.pathname === `/api/workspace/v1/items/${item.item_id}/original` && !url.hash
    && [...url.searchParams.keys()].length === 1 && url.searchParams.get('project_id') === project;
}

export function extractionCopy(value, expected) {
  if (value?.id !== expected.id || value.state !== 'retained' || !positive(value.revision)
    || value.target_project_id !== expected.target_project_id || value.target_recognition_id !== expected.target_recognition_id
    || !Array.isArray(value.materials) || value.materials.some(item => !identity(item?.item_id)
      || item.document_id !== null && item.document_id !== undefined && !identity(item.document_id)
      || typeof item.title !== 'string' || typeof item.text !== 'string' || typeof item.private !== 'boolean'
      || !Array.isArray(item.files) || item.files.some(file => !personalFile(file, item, value.target_project_id)))) invalid();
  return value;
}

export function extractionTargets(value) {
  if (!Array.isArray(value?.items) || value.items.some(item => !identity(item?.id) || typeof item.name !== 'string')) invalid();
  return value.items.filter(item => !['me', 'inbox'].includes(item.id) && item.shared !== true && item.scope_kind !== 'shared');
}

export function completeExtractionSelection(preview, selected) {
  return Array.isArray(selected) && unique(selected) && selected.length === preview.recognition_ids.length
    && preview.recognition_ids.every(id => selected.includes(id));
}
