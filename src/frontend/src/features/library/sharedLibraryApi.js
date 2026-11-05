import { productFetch, deviceIdentity, isDeviceIdentity, userStorageKey } from '../../shared/api/deviceTransport';
import { readResponseJson } from '../../shared/lib/responseJson';
import { libraryBackendUrl } from '../rebuild/libraryOverviewTransport';

const names = { insight: 'insights', summary: 'summaries', note: 'notes', source: 'sources' };
const part = encodeURIComponent;
const invalid = () => { throw new Error('共享资料范围未核对 · 重读'); };
const revision = (value, minimum = 1) => Number.isSafeInteger(value) && value >= minimum;
export function sharedLibraryScope(value, namespace) {
  if (typeof value?.namespace !== 'string' || !value.namespace
      || namespace !== undefined && value.namespace !== namespace) invalid();
  return value;
}
function authority(value) {
  if (!value || Object.keys(value).sort().join(',') !== 'account_revision,member_revision,project_revision'
      || !revision(value.account_revision) || !revision(value.member_revision, 0) || !revision(value.project_revision)) invalid();
  return value;
}
export function sharedLibraryList(value, layer, project) {
  sharedLibraryScope(value);
  authority(value.authority);
  if (!names[layer] || !Array.isArray(value.items) || typeof value.can_write !== 'boolean') invalid();
  for (const row of value.items) {
    if (!row || typeof (row.id || row.document_id) !== 'string') invalid();
    if (layer === 'insight' && (row.project_id !== project || row.namespace !== value.namespace)) invalid();
  }
  return value;
}
export function sharedLibraryDetail(value, project) {
  sharedLibraryScope(value);
  authority(value.authority);
  if (typeof value.can_write !== 'boolean' || typeof value.can_edit_insight !== 'boolean'
      || value.insight && (value.insight_project_id !== project || !revision(value.insight.recall_preference_revision, 0))) invalid();
  return value;
}
export function sharedInsightMutation(value, detail, project) {
  sharedLibraryDetail(detail, project);
  sharedLibraryScope(value, detail.namespace); authority(value.authority);
  const row = value.insight;
  if (value.can_write !== true || !row || row.id !== detail.insight?.id || row.project_id !== project
      || row.namespace !== detail.namespace || typeof row.text !== 'string' || !Array.isArray(row.conditions)
      || row.conditions.some(condition => typeof condition !== 'string') || !revision(row.revision)
      || row.revision < detail.insight.revision || !revision(row.recall_preference_revision, 0)
      || !['candidate', 'recognition'].includes(row.kind) || !['pending', 'active', 'forgotten', 'stale'].includes(row.state)) invalid();
  return { ...detail, insight: row, namespace: value.namespace, authority: value.authority,
    can_write: true, can_edit_insight: ['pending', 'active'].includes(row.state), needs_detail_read: true };
}
export function sharedInsightEditorApi(project, detail) {
  let current = sharedLibraryDetail(detail, project);
  const actor = deviceIdentity(), target = userStorageKey('shared-insight-editor'), namespace = detail.namespace;
  const identity = detail.insight?.id;
  function assertScope() {
    if (!isDeviceIdentity(actor) || userStorageKey('shared-insight-editor') !== target)
      throw new Error('空间已变化 · 重新读取');
    sharedLibraryScope(current, namespace);
  }
  return {
    currentDetail: () => { assertScope(); return current; },
    drill: async (targetProject, layer, id, options) => {
      assertScope();
      if (targetProject !== project || layer !== 'insight' || id !== identity) invalid();
      const value = await sharedLibraryApi.drill(targetProject, layer, id, options);
      assertScope(); sharedLibraryScope(value, namespace);
      current = value; return value;
    },
    edit: async (target, row, text, conditions) => {
      assertScope();
      if (target !== project || row.id !== identity) invalid();
      const saved = await sharedLibraryApi.edit(project, row, text, conditions, current);
      assertScope(); current = saved;
      return current.insight;
    },
  };
}
function mutationInput(project, row, detail) {
  sharedLibraryDetail(detail, project);
  if (!detail.can_write || detail.insight?.id !== row.id || detail.insight.revision !== row.revision) invalid();
  return { namespace: detail.namespace, authority: detail.authority, expected_revision: row.revision };
}
export function sharedSupersessionCommand(project, detail, target) {
  const row = detail?.insight;
  if (!row) invalid();
  const { namespace, expected_revision } = mutationInput(project, row, detail);
  if (!detail.can_edit_insight || row.kind !== 'candidate' || row.state !== 'pending'
      || target?.kind !== 'recognition' || target.state !== 'active' || target.project_id !== project
      || target.namespace !== namespace || !revision(target.revision) || typeof target.id !== 'string'
      || !target.id || target.id === row.id) invalid();
  return { candidate_id: row.id, namespace, expected_revision, target_id: target.id, target_revision: target.revision };
}
function supersessionResult(value, command, id) {
  sharedLibraryScope(value, command.namespace); authority(value.authority);
  if (value.can_write !== true || typeof id !== 'string' || !id || id === command.candidate_id
      || value.target?.id !== command.target_id || value.target.revision !== command.target_revision
      || value.target.superseded_by !== id || value.relation?.from_id !== id
      || value.relation.to_id !== command.target_id || value.relation.relation !== 'supersedes'
      || typeof value.relation.id !== 'string' || !value.relation.id) invalid();
  return value;
}
async function request(project, path, options = {}) {
  const response = await productFetch(libraryBackendUrl(`/api/v2/server/shared-projects/${part(project)}/library/${path}`),
    { cache: 'no-store', ...options });
  const value = await readResponseJson(response);
  if (!response.ok) {
    const error = new Error(response.status === 409 ? '资格或内容已变化 · 重读'
      : response.status === 403 ? '当前没有操作权限' : response.status === 401 ? '登录已失效 · 重新登录'
      : response.status === 429 ? '空间额度不足 · 联系管理员' : '读取未完成 · 重试');
    error.status = response.status; error.code = value?.detail;
    throw error;
  }
  return value;
}
export function sharedInsightMergeApi(project, namespace, currentAuthority) {
  const base = 'insight-merges';
  function validate(value) {
    sharedLibraryScope(value, namespace); authority(value.authority);
    if (value.project_id !== project || value.can_write !== true) invalid();
    return value;
  }
  function write(path, input) {
    return request(project, base + path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...input, namespace, authority: authority(currentAuthority) }) }).then(validate);
  }
  const query = '?' + new URLSearchParams({ namespace });
  return {
    targets: signal => request(project, base + '/targets' + query, { signal }).then(value => {
      validate(value);
      if (!Array.isArray(value.items) || value.items.some(row => row.project_id !== project || row.namespace !== namespace
          || row.kind !== 'recognition' || row.state !== 'active' || row.merge_eligible !== true || !revision(row.revision))) invalid();
      return value.items;
    }),
    active: input => write('/active', input),
    prepare: input => write('/prepare', input),
    read: (_project, id, signal) => request(project, base + '/' + part(id) + query, { signal }).then(validate),
    cancel: (_project, id) => write('/' + part(id) + '/cancel', { expected_revision: 0 }),
    review: (id, expectedRevision, decision) => write('/' + part(id) + '/review', {
      expected_revision: expectedRevision, decision }),
  };
}
function submitSupersession(project, command, access) {
  const { candidate_id, ...input } = command;
  return request(project, `insights/${part(candidate_id)}/supersede`, { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...input, authority: authority(access) }),
  }).then(value => {
    const row = value.insight;
    if (row?.project_id !== project || row.namespace !== input.namespace || row.kind !== 'recognition'
        || row.state !== 'active' || !revision(row.revision) || !Array.isArray(row.aliases)
        || !row.aliases.includes(candidate_id)) invalid();
    return supersessionResult(value, command, row.id);
  });
}
export const sharedLibraryApi = {
  supersessionTargets: (project, detail, { signal } = {}) => {
    const row = detail?.insight;
    if (!row || !detail.can_edit_insight || row.kind !== 'candidate' || row.state !== 'pending') invalid();
    const { namespace, expected_revision } = mutationInput(project, row, detail);
    return request(project, `insights/${part(row.id)}/supersession-targets?${new URLSearchParams({ namespace, expected_revision })}`, { signal })
      .then(value => {
        sharedLibraryScope(value, namespace); sharedLibraryList(value, 'insight', project);
        if (value.can_write !== true || value.items.some(item => item.kind !== 'recognition' || item.state !== 'active'
            || item.id === row.id || !revision(item.revision) || typeof item.text !== 'string')) invalid();
        return value.items;
      });
  },
  supersede: (project, detail, target) => submitSupersession(project,
    sharedSupersessionCommand(project, detail, target), detail.authority),
  retrySupersession: (project, command, qualification) => {
    sharedLibraryScope(qualification, command.namespace);
    if (qualification.command !== command || qualification.state !== 'pending' || qualification.can_write !== true) invalid();
    return submitSupersession(project, command, qualification.authority);
  },
  supersession: (project, command) => {
    const { candidate_id, ...input } = command;
    return request(project, `insights/${part(candidate_id)}/supersede?${new URLSearchParams(input)}`).then(value => {
      if (value.state === 'applied') return supersessionResult(value, command, value.insight_id);
      sharedLibraryScope(value, command.namespace); authority(value.authority);
      if (!['pending', 'changed'].includes(value.state) || value.can_write !== true) invalid();
      return { ...value, command };
    });
  },
  edit: (project, row, text, conditions, detail) => {
    const input = mutationInput(project, row, detail);
    if (!detail.can_edit_insight || !['pending', 'active'].includes(row.state)) invalid();
    return request(project, `insights/${part(row.id)}`, { method: 'PATCH',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...input, text, conditions }),
    }).then(value => sharedInsightMutation(value, detail, project));
  },
  forget: (project, row, forgotten, detail) => {
    const input = mutationInput(project, row, detail);
    if (typeof forgotten !== 'boolean' || !revision(row.recall_preference_revision, 0)
        || row.kind === 'candidate' && (forgotten || row.state !== 'forgotten')
        || !['candidate', 'recognition'].includes(row.kind)) invalid();
    return request(project, `insights/${part(row.id)}/forget`, { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...input, forgotten,
        preference_revision: row.recall_preference_revision }),
    }).then(value => sharedInsightMutation(value, detail, project));
  },
  list: (layer, project, { scene, q = '', state, signal } = {}) => request(project,
    `${names[layer]}?${new URLSearchParams({ ...(scene ? { scene } : {}), q, ...(state ? { state } : {}) })}`, { signal })
    .then(value => sharedLibraryList(value, layer, project)),
  drill: (project, layer, id, { document_id, source_id, signal } = {}) => request(project,
    `${part(layer)}/${part(id)}?${new URLSearchParams({ ...(document_id ? { document_id } : {}), ...(source_id ? { source_id } : {}) })}`, { signal })
    .then(value => {
      sharedLibraryDetail(value, project);
      if (layer === 'insight' && value.insight?.id !== id) invalid();
      return value;
    }),
  sourceText: (project, id, { signal } = {}) => request(project, `sources/${part(id)}/text`, { signal })
    .then(value => {
      sharedLibraryScope(value);
      if (value.project_id !== project || value.id !== id || typeof value.text !== 'string') invalid();
      return value;
    }),
  images: (project, id, { signal } = {}) => request(project, `sources/${part(id)}/images`, { signal })
    .then(value => {
      sharedLibraryScope(value);
      if (value.project_id !== project || !Array.isArray(value.images) || value.images.some((image, index) =>
        image.ordinal !== index + 1 || typeof image.name !== 'string'
        || image.url !== `/api/v2/server/shared-projects/${project}/library/sources/${id}/images/${image.ordinal}`)) invalid();
      return value;
    }),
  review: (project, row, action, detail) => {
    sharedLibraryDetail(detail, project);
    if (!detail.can_write || !detail.can_edit_insight || detail.insight?.id !== row.id
        || detail.insight.revision !== row.revision || row.kind !== 'candidate' || row.state !== 'pending'
        || !['confirm', 'drop'].includes(action)) invalid();
    return request(project, `insights/${part(row.id)}/review`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: action === 'drop' ? 'discard' : action, expected_revision: row.revision,
        authority: detail.authority }),
    }).then(value => sharedLibraryScope(value, detail.namespace));
  },
};
