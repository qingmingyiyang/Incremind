import { productFetch } from '../../shared/api/deviceTransport';
import { readResponseJson } from '../../shared/lib/responseJson';
import { libraryBackendUrl } from '../rebuild/libraryOverviewTransport';
import { readSharedCommands, saveSharedCommand, clearSharedCommand } from '../shared-projects/sharedProjectCommands';

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const revision = value => Number.isSafeInteger(value) && value > 0;

async function request(path, { method = 'GET', body, signal } = {}) {
  const response = await productFetch(libraryBackendUrl('/api/v2/library/insight-merges' + path), {
    method, signal, cache: 'no-store', headers: { Accept: 'application/json', ...(body ? { 'Content-Type': 'application/json' } : {}) },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  const value = await readResponseJson(response);
  if (!response.ok) {
    const error = new Error(response.status === 404 ? '尚未读到受理记录' : response.status === 409 ? '合并资格已变化 · 重新读取' : '合并状态未核对');
    error.status = response.status; throw error;
  }
  return value;
}

export const libraryMergeApi = {
  prepare: body => request('/prepare', { method: 'POST', body }),
  read: (project, id, signal) => request(`/${encodeURIComponent(id)}?${new URLSearchParams({ project_id: project })}`, { signal }),
  cancel: (project, id) => request(`/${encodeURIComponent(id)}/cancel`, { method: 'POST', body: { project_id: project, expected_revision: 0 } }),
};

export function mergeCommands(scope, project) {
  return readSharedCommands(scope).map(command => {
    const data = command.body;
    if (!uuid.test(command.id) || command.path !== 'insight-merges' || data.kind !== 'library-merge'
        || Object.keys(data).sort().join(',') !== 'candidate_id,candidate_revision,kind,project_id,target_id,target_revision'
        || data.project_id !== project || typeof data.candidate_id !== 'string' || !data.candidate_id
        || typeof data.target_id !== 'string' || !data.target_id || !revision(data.candidate_revision)
        || !revision(data.target_revision) || JSON.stringify(command.expected) !== JSON.stringify(data))
      throw new Error('合并操作记录无法核对 · 保留浏览器资料');
    return command;
  });
}

export function beginMergeCommand(scope, project, candidate, target) {
  if (mergeCommands(scope, project).length) throw new Error('已有合并操作待核对');
  const body = { kind: 'library-merge', project_id: project, candidate_id: candidate.id,
    candidate_revision: candidate.revision, target_id: target.id, target_revision: target.revision };
  const command = { version: 1, storageKey: scope, id: crypto.randomUUID(), path: 'insight-merges',
    body, expected: body, phase: 'prepared', createdAt: new Date().toISOString() };
  saveSharedCommand(command);
  return command;
}

export function validateMergeOperation(operation, command) {
  const input = command.body;
  if (operation?.operation_id !== command.id || operation.project_id !== input.project_id || !revision(operation.revision)
      || !['prepared', 'cancelled'].includes(operation.state)) throw new Error('合并操作身份无法核对');
  if (operation.state === 'cancelled') {
    if (operation.proposal !== null) throw new Error('取消记录无法核对');
    return operation;
  }
  const proposal = operation.proposal, snapshot = proposal?.snapshot;
  if (proposal?.id !== 'library-merge-' + command.id.replaceAll('-', '') || !revision(proposal.revision)
      || proposal.scope?.project_id !== input.project_id || proposal.scope.user_id !== 'local-user'
      || !['pending', 'approved', 'rejected'].includes(proposal.state)
      || snapshot?.schema_version !== 2 || snapshot.pending_output !== true || snapshot.project_id !== input.project_id
      || snapshot.scope?.user_id !== 'local-user' || snapshot.scope.project_id !== input.project_id
      || !Array.isArray(snapshot.target_recognition_ids) || snapshot.target_recognition_ids.length !== 2
      || ![input.candidate_id,input.target_id].every(id=>snapshot.target_recognition_ids.includes(id))
      || !snapshot.candidates?.some(row => row.id === input.candidate_id && row.revision === input.candidate_revision)
      || !snapshot.recognitions?.some(row => row.id === input.target_id && row.revision === input.target_revision)
      || !Array.isArray(proposal.outputs) || proposal.outputs.length !== 1
      || typeof proposal.outputs[0].content !== 'string' || !Array.isArray(proposal.outputs[0].conditions)
      || proposal.outputs[0].conditions.some(value => typeof value !== 'string'))
    throw new Error('合并方案来源无法核对');
  if (proposal.state === 'approved' && (!Array.isArray(proposal.result_candidate_ids)
      || proposal.result_candidate_ids.length !== 1 || typeof proposal.result_candidate_ids[0] !== 'string'
      || !proposal.result_candidate_ids[0])) throw new Error('合并结果无法核对');
  return operation;
}

export { saveSharedCommand as markMergeCommand, clearSharedCommand as finishMergeCommand };
