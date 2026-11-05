import { productFetch } from '../../shared/api/deviceTransport';
import { readResponseJson } from '../../shared/lib/responseJson';
import { libraryBackendUrl } from '../rebuild/libraryOverviewTransport';

const base = '/api/v2/server/conversation-shares';
const part = encodeURIComponent;
const messages = {
  conversation_source_revision_changed: '原对话已变化 · 重新预览',
  conversation_share_source_changed: '原对话或资料已变化 · 重新读取',
  conversation_source_unavailable: '当前对话快照不可用',
  conversation_share_unavailable: '对话分享已撤回',
  conversation_share_already_filed: '对方已归档',
  conversation_share_cannot_file: '对话分享当前不可归档',
  conversation_share_revision_changed: '分享状态已变化 · 重新读取',
  conversation_recipient_unavailable: '接收账号当前不可用',
  conversation_archive_project_unavailable: '归档项目当前不可用',
  conversation_citation_static: '当前仅可读取引用片段',
  conversation_citation_changed: '原引用已变化',
  conversation_share_requires_check: '结果待核对',
  shared_authority_changed: '共享项目写入权限已变化',
  shared_conversation_destination_changed: '共享资料位置已变化',
};

async function request(path, { body, key, signal } = {}) {
  const response = await productFetch(libraryBackendUrl(base + path), {
    method: body === undefined ? 'GET' : 'POST', cache: 'no-store', signal,
    headers: { ...(body === undefined ? {} : { 'Content-Type': 'application/json' }), ...(key ? { 'Idempotency-Key': key } : {}) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const value = await readResponseJson(response);
  if (!response.ok) {
    const code = value?.detail;
    const error = new Error(messages[code] || (response.status === 403 ? '当前没有读取或操作资格'
      : response.status === 401 ? '请重新登录' : '对话分享操作未完成'));
    error.status = response.status; error.code = code;
    throw error;
  }
  return value;
}

export const conversationShareApi = {
  preview: ({ turnId, projectId, scopeKind }, signal) => request(
    `/preview/${part(turnId)}?${new URLSearchParams({ project_id: projectId, scope_kind: scopeKind })}`, { signal }),
  list: (side, { turnId, signal } = {}) => request(`?${new URLSearchParams({ side, ...(turnId ? { turn_id: turnId } : {}) })}`, { signal }),
  detail: (id, signal) => request(`/${part(id)}`, { signal }),
  citation: (id, ordinal, signal) => request(`/${part(id)}/citations/${ordinal}`, { signal }),
  command: (key, signal) => request(`/commands/${part(key)}`, { signal }),
  execute: (command, signal) => request(command.path, { body: command.body, key: command.id, signal }),
  projectTarget: (project, signal) => request(`/projects/${part(project)}`, { signal }),
  projectCommand: (project, key, namespace, signal) => request(
    `/projects/${part(project)}/commands/${part(key)}?${new URLSearchParams({ namespace })}`, { signal }),
  sendToProject: (project, command, signal) => request(`/projects/${part(project)}`, { body: command.body, key: command.id, signal }),
};
