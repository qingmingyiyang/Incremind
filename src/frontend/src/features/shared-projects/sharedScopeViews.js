import { readThreadHistory } from '../../shared/lib/threadHistory';

const identity = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/.test(value);
const invalid = () => { throw new Error('共享项目范围未核对，请重新读取'); };

export function sharedScopes(value) {
  if (!Array.isArray(value?.items) || new Set(value.items.map(row => row?.id)).size !== value.items.length) invalid();
  for (const row of value.items) {
    if (!identity(row?.id) || typeof row.name !== 'string' || row.scope_kind !== 'shared'
      || !['active', 'unavailable'].includes(row.membership)
      || ![true, false].includes(row.can_ask) || ![true, false].includes(row.can_write)
      || row.can_do !== undefined && ![true, false].includes(row.can_do)
      || ![true, false].includes(row.can_read_sources) || ![true, false].includes(row.has_personal_history)
      || (row.membership === 'active' ? !['owner', 'member', 'viewer', 'admin'].includes(row.role)
        || !row.can_read_sources || row.can_write !== ['owner', 'member', 'admin'].includes(row.role)
        : row.role !== null || row.can_ask || row.can_do || row.can_write || row.can_read_sources)
      || !Array.isArray(row.threads) || row.has_personal_history !== Boolean(row.threads.length)
      || new Set(row.threads.map(thread => thread?.id)).size !== row.threads.length
      || row.threads.some(thread => !identity(thread?.id) || typeof thread.title !== 'string'
        || typeof thread.updated_at !== 'string' || !Number.isFinite(Date.parse(thread.updated_at)))) invalid();
  }
  return value.items;
}

export function sharedHistoryThread(scope, threadId, turnId, expectedProject) {
  if (!scope || scope.scope_kind !== 'shared' || scope.id !== expectedProject) invalid();
  if (threadId) {
    if (!scope.threads.some(row => row.id === threadId)) throw new Error('指定对话不在本人的共享历史中');
    return threadId;
  }
  return turnId ? null : scope.threads[0]?.id || null;
}

export function sharedHistoryDetail(value, threadId, projectId) {
  return readThreadHistory(value, threadId, projectId, 'shared');
}
