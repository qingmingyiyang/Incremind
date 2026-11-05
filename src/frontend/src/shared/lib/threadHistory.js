const identity = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/.test(value);
const invalid = () => { const error = new Error('对话范围或身份未核对，请重新读取'); error.code = 'thread_scope_changed'; throw error; };

export function threadHistoryQuery(projectId, scopeKind = 'personal') {
  if (!identity(projectId) || !['personal', 'shared'].includes(scopeKind)) invalid();
  return new URLSearchParams({ project_id: projectId, scope_kind: scopeKind }).toString();
}

export function readScopedTurn(value, scopeKind = 'personal') {
  if (!['personal', 'shared'].includes(scopeKind) || !identity(value?.id) || value.scope_kind !== scopeKind) invalid();
  return value;
}

export function readThreadHistory(value, threadId, projectId, scopeKind = 'personal') {
  threadHistoryQuery(projectId, scopeKind);
  if (!identity(threadId) || value?.id !== threadId || value.scope_kind !== scopeKind || !Array.isArray(value.turns)
    || new Set(value.turns.map(turn => turn?.id)).size !== value.turns.length
    || value.turns.some(turn => !identity(turn?.id) || turn.scope_kind !== scopeKind
      || turn.thread_id !== undefined && turn.thread_id !== threadId || turn.project_id !== undefined && turn.project_id !== projectId
      || !['ask', 'do', 'remember', 'multi', 'inspiration'].includes(turn.intent)
      || !turn.receipt || typeof turn.receipt !== 'object' || Array.isArray(turn.receipt)
      || turn.user_text !== undefined && turn.user_text !== null && typeof turn.user_text !== 'string')) invalid();
  return value;
}
