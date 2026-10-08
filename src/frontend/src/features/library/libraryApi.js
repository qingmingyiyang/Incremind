import { productFetch as fetch } from '../../shared/api/deviceTransport';
import { readResponseJson } from '../../shared/lib/responseJson';
import { recognitionApi } from '../../shared/api/recognitionApi';
import { libraryBackendUrl } from '../rebuild/libraryOverviewTransport';

async function request(path, options = {}, expectNoContent = false) {
  const response = await fetch(libraryBackendUrl(`/api/v2/library/${path}`), { cache: 'no-store', ...options });
  if (expectNoContent && response.status === 204) return;
  const value = await readResponseJson(response);
  if (!response.ok) {
    const error = new Error(response.status === 409 ? '内容已变化 · 刷新' : '读取未完成 · 重试');
    error.status = response.status;
    if (value?.detail === 'consolidate_limit') error.code = 'consolidate_limit';
    throw error;
  }
  return value;
}
const mutation = (path, body, method = 'POST') => request(path, {
  method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});
const names = { insight: 'insights', summary: 'summaries', note: 'notes', source: 'sources' };
const skillPath = (project, suffix = '') => `/api/v2/projects/${encodeURIComponent(project)}/skill-exports${suffix}`;
async function skillRequest(project, suffix = '', options = {}, binary = false) {
  const response = await fetch(libraryBackendUrl(skillPath(project, suffix)), { cache: 'no-store', ...options });
  if (binary && response.ok) return response.blob();
  const value = await readResponseJson(response);
  if (!response.ok) {
    const error = new Error(response.status === 409 ? '内容已变化 · 重新读取' : response.status === 403 ? '外发已关闭 · 可手写' : '操作未完成 · 重试');
    error.status = response.status; throw error;
  }
  return value;
}
const skillMutation = (project, suffix, body, method = 'POST', binary = false) => skillRequest(project, suffix, {
  method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
}, binary);
export const libraryApi = {
  gaps(project, { signal } = {}) {
    return request(`gaps?${new URLSearchParams({ project_id: project })}`, { signal });
  },
  dismissGap(project, id, { signal } = {}) {
    return request(`gaps/${encodeURIComponent(id)}/dismiss`, { method: 'POST', signal, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project_id: project }) }, true);
  },
  outcomeChoices: (project, { signal } = {}) => request(`outcomes?${new URLSearchParams({ project_id: project })}`, { signal }),
  outcomeVersions: (project, id, { signal } = {}) => request(`outcomes/${encodeURIComponent(id)}/versions?${new URLSearchParams({ project_id: project })}`, { signal }),
  skillExports: (project, { signal } = {}) => skillRequest(project, '', { signal }),
  skillExportMethods: (project, { scene, signal } = {}) => skillRequest(project, `/methods${scene ? `?${new URLSearchParams({ scene })}` : ''}`, { signal }),
  skillExport: (project, id, { signal } = {}) => skillRequest(project, `/${encodeURIComponent(id)}`, { signal }),
  createSkillExport: (project, body) => skillMutation(project, '', body),
  generateSkillExport: (project, body) => skillMutation(project, '/generate', body),
  saveSkillExport: (project, id, revision, document) => skillMutation(project, `/${encodeURIComponent(id)}`, { expected_revision: revision, document }, 'PATCH'),
  reviewSkillExport: (project, id, revision) => skillMutation(project, `/${encodeURIComponent(id)}/review`, { expected_revision: revision }),
  downloadSkillExport: (project, id, revision) => skillMutation(project, `/${encodeURIComponent(id)}/download`, { expected_revision: revision }, 'POST', true),
  regenerateSkillExport: (project, id, body) => skillMutation(project, `/${encodeURIComponent(id)}/regenerate`, body),
  folderSkillExport: (project, id, body) => skillMutation(project, `/${encodeURIComponent(id)}/folder`, body),
  signalReviews(project, { signal } = {}) {
    return request(`signal-reviews?${new URLSearchParams({ project_id: project })}`, { signal });
  },
  decideSignalReviews(project, items, { signal } = {}) {
    return request('signal-reviews/decide', { method: 'POST', signal, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project_id: project, items }) });
  },
  consolidation: (project, { signal } = {}) => request(`consolidate?${new URLSearchParams({ project_id: project })}`, { signal }),
  consolidate: project => mutation('consolidate', { project_id: project }),
  fileDocument: (project,row,target,scene) => mutation(`notes/${encodeURIComponent(row.document_id)}/file`,{project_id:project,target_project_id:target,scene,expected_revision:row.revision}),
  unfileDocument: (project,id,revision) => mutation(`notes/${encodeURIComponent(id)}/unfile`,{project_id:project,expected_revision:revision}),
  documentScene: (project,row,scene,assignment) => mutation(`notes/${encodeURIComponent(row.document_id)}/scene`,{project_id:project,scene,expected_revision:row.revision,assignment_revision:assignment},'PATCH'),
  inboxProjects: ({signal}={}) => request('inbox/project-suggestions',{signal}),
  sourcePrivacy: (project, id, options = {}) => request(`sources/${encodeURIComponent(id)}/privacy?${new URLSearchParams({project_id:project})}`, options),
  setSourcePrivacy: (project, id, state, privateState) => mutation(`sources/${encodeURIComponent(id)}/privacy`, {
    project_id:project, source_revision:state.source_revision, policy_revision:state.policy_revision,
    allowed_purposes:privateState ? [] : ['generation','embedding','rerank'],
  }, 'PUT'),
  links: (project, id, { signal } = {}) => request(`insights/${encodeURIComponent(id)}/links?${new URLSearchParams({ project_id: project })}`, { signal }),
  // The existing proposal read model supplies its CAS revision; the links DTO stays unchanged.
  linkProposals: (project, { signal } = {}) => recognitionApi.listRelationProposals({
    projectId: project, fetchImpl: (url, options) => fetch(url, { ...options, signal }),
  }),
  evidenceSupport: (project,id,options={}) => request(`insights/${encodeURIComponent(id)}/evidence-support?${new URLSearchParams({project_id:project})}`,options),
  reviewEvidenceSupport: (project,id,revision,accept) => mutation(`evidence-support/${encodeURIComponent(id)}/${accept?'accept':'dismiss'}`,{project_id:project,expected_revision:revision}),
  reviewLink: (project, id, revision, accept) => mutation(`link-suggestions/${encodeURIComponent(id)}/${accept ? 'accept' : 'dismiss'}`, { project_id: project, expected_revision: revision }),
  opened: async (project, layer, id) => {
    if (!['insight', 'summary', 'note', 'document'].includes(layer)) return;
    try {
      await fetch(libraryBackendUrl('/api/v2/usage/open'), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_id: project, kind: layer, id }),
      });
    } catch { /* Usage recording must not interrupt opening the material. */ }
  },
  list: (layer, project, { scene, q = '', signal } = {}) => request(`${names[layer]}?${new URLSearchParams({ project_id: project, ...(scene ? { scene } : {}), q })}`, { signal }),
  drill: (project, layer, id, { document_id, source_id } = {}) => request(`drill?${new URLSearchParams({ project_id: project, from: layer, id, ...(document_id ? { document_id } : {}), ...(source_id ? { source_id } : {}) })}`),
  sourceText: (project, id, { signal } = {}) => request(`sources/${encodeURIComponent(id)}/text?${new URLSearchParams({ project_id: project })}`, { signal }),
  review: (project, row, action) => mutation(`insights/${encodeURIComponent(row.id)}/${action}`, { project_id: project, expected_revision: row.revision }),
  forget: (project, row, forgotten) => mutation(`insights/${encodeURIComponent(row.id)}/forget`, { project_id: project, forgotten }),
  edit: (project, row, text, conditions) => mutation(`insights/${encodeURIComponent(row.id)}`, { project_id: project, expected_revision: row.revision, text, conditions }, 'PATCH'),
  verify: (project, row) => mutation(`notes/${encodeURIComponent(row.document_id)}/verify`, { project_id: project, document_revision: row.revision }),
  restoreRecall: (project, row) => mutation(`notes/${encodeURIComponent(row.document_id)}/restore-recall`, {
    project_id: project, document_revision: row.revision, preference_revision: row.recall_preference_revision,
  }),
  inboxSuggestions: (project, { signal } = {}) => request(`inbox/suggestions?${new URLSearchParams({ target_project_id: project })}`, { signal }),
  fileInbox: (project, row, scene) => mutation(`inbox/insight/${encodeURIComponent(row.id)}/file`, { target_project_id: project, scene, expected_revision: row.revision }),
  confirmTo: (project, row, target) => mutation(`inbox/insight/${encodeURIComponent(row.id)}/file`, {
    source_project_id: project, target_project_id: target, expected_revision: row.revision, confirm: true,
  }),
};
