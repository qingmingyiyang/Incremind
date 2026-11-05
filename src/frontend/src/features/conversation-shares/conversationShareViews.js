const segment = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/.test(value);
const revision = value => Number.isSafeInteger(value) && value > 0;
const ordinal = value => Number.isSafeInteger(value) && value >= 0;
const kinds = ['personal', 'shared'];
const states = ['received', 'filing', 'filed', 'revoked'];
const invalid = () => { throw new Error('对话分享身份未核对 · 重新读取'); };

export function sameShareSource(row, source) {
  return Boolean(row && segment(source?.turnId) && segment(source.projectId) && kinds.includes(source.scopeKind)
    && row.turn_id === source.turnId && row.project_id === source.projectId && row.scope_kind === source.scopeKind);
}

function readSnapshot(snapshot) {
  if (typeof snapshot?.question !== 'string' || typeof snapshot.answer !== 'string' || !Array.isArray(snapshot.citations)
      || snapshot.citations.some(row => typeof row?.title !== 'string' || typeof row.quote !== 'string')) invalid();
  return snapshot;
}

function sourceRevision(value) {
  return revision(value?.turn_revision) && segment(value.content?.id) && revision(value.content.revision)
    && Object.keys(value).length === 2 && Object.keys(value.content).length === 2;
}

export function conversationSharePreview(value) {
  if (!sourceRevision(value?.source_revision)) invalid();
  readSnapshot(value.snapshot);
  return value;
}

export function sameConversationPreview(left, right) {
  if (!left || !right) return false;
  const original = left.source_revision, current = right.source_revision;
  const before = left.snapshot, after = right.snapshot;
  return original.turn_revision === current.turn_revision && original.content.id === current.content.id
    && original.content.revision === current.content.revision
    && before.question === after.question && before.answer === after.answer && before.citations.length === after.citations.length
    && before.citations.every((citation, index) => citation.title === after.citations[index].title && citation.quote === after.citations[index].quote);
}

export function conversationShareRow(value, side, { id, detail = false } = {}) {
  if (!segment(value?.id) || id && value.id !== id || value.direction !== side || !revision(value.revision)
      || !states.includes(value.state) || !segment(value.user_id) || value.user_name !== null && typeof value.user_name !== 'string'
      || typeof value.created_at !== 'string' || value.can_file !== (side === 'inbox' && value.state === 'received')
      || value.can_revoke !== (side === 'sent' && ['received', 'filing'].includes(value.state))) invalid();
  if ((side === 'sent' || value.state !== 'revoked') && typeof value.title !== 'string') invalid();
  if (side === 'sent' && (!segment(value.source?.turn_id) || !segment(value.source.project_id)
      || !kinds.includes(value.source.scope_kind))) invalid();
  if (side === 'inbox' && value.state === 'filed'
      && (!segment(value.archive?.project_id) || !segment(value.archive.item_id))) invalid();
  if (side === 'inbox' && value.state === 'filing'
      && (!segment(value.filing?.command_key) || !segment(value.filing.project_id) || !revision(value.filing.expected_revision))) invalid();
  if (detail && value.state !== 'revoked') {
    readSnapshot(value.snapshot);
    if (side === 'inbox' && (!Array.isArray(value.citation_details) || new Set(value.citation_details).size !== value.citation_details.length
        || value.citation_details.some(index => !ordinal(index) || index >= value.snapshot.citations.length))) invalid();
  }
  return value;
}

export function conversationShareList(value, side) {
  if (!Array.isArray(value?.items) || new Set(value.items.map(row => row?.id)).size !== value.items.length) invalid();
  return value.items.map(row => conversationShareRow(row, side));
}

function sharedNamespace(value, projectId) {
  if (typeof value !== 'string') return false;
  const parts = value.split('/');
  return parts.length === 3 && parts[0] === 'shared' && parts[1] === projectId && segment(parts[2]);
}

function projectAuthority(value) {
  return Boolean(value && Object.keys(value).sort().join(',') === 'account_revision,member_revision,project_revision'
    && revision(value.account_revision) && revision(value.project_revision)
    && Number.isSafeInteger(value.member_revision) && value.member_revision >= 0);
}

export function conversationProjectTarget(value, target) {
  if (!segment(target) || value?.project_id !== target || value.scope_kind !== 'shared'
      || !sharedNamespace(value.namespace, target) || !projectAuthority(value.authority)) invalid();
  return value;
}

export function conversationProjectCommand(command, source) {
  const body = command?.body, target = command?.expected?.projectId;
  if (!segment(command?.id) || command.id.length > 64 || command.expected?.action !== 'share-project'
      || !segment(target) || command.path !== `/projects/${target}`
      || !body || Object.keys(body).length !== 7 || !sameShareSource(body, source) || !sourceRevision(body.source_revision)
      || !sharedNamespace(body.namespace, target) || !projectAuthority(body.authority)
      || !Array.isArray(body.include_citations)
      || body.include_citations.some((index, position) => !ordinal(index) || position > 0 && body.include_citations[position - 1] >= index)) invalid();
  return command;
}

export function conversationProjectPointer(value) {
  if (!segment(value?.id) || value.id.length > 64 || !segment(value.projectId)
      || !sharedNamespace(value.namespace, value.projectId) || Object.keys(value).length !== 3) invalid();
  return value;
}

export function conversationProjectReceipt(value, pointer) {
  if (value?.state !== 'accepted' || value.command_id !== pointer.id || value.project_id !== pointer.projectId
      || value.scope_kind !== 'shared' || value.namespace !== pointer.namespace || value.kind !== 'conversation'
      || value.item?.input_kind !== 'conversation' || !segment(value.item.id)) invalid();
  return { projectId: pointer.projectId, namespace: pointer.namespace, itemId: value.item.id };
}

export function conversationProjectNotAccepted(value) {
  return value?.state === 'not_accepted' && Object.keys(value).length === 1;
}

export function pendingConversationProjectCommand(commands, target, namespace) {
  return commands.find(command => command.expected.projectId === target && command.body.namespace === namespace) || null;
}

export function conversationSourceRevisionAdvanced(original, current) {
  return sourceRevision(original) && sourceRevision(current)
    && (current.turn_revision > original.turn_revision
      || current.turn_revision === original.turn_revision && current.content.id === original.content.id
        && current.content.revision > original.content.revision);
}

export function conversationCitationAllowed(share, index) {
  return share?.state !== 'revoked' && share?.citation_details?.includes(index) === true;
}

export function restrictCommandCitations(share, detail) {
  if (share.state === 'revoked') return share;
  const citation_details = share.citation_details.filter(index => detail?.id === share.id && conversationCitationAllowed(detail, index));
  return { ...share, citation_details };
}

export function conversationCitation(value) {
  if (value?.scope_kind !== 'shared' || !segment(value.project_id) || !sharedNamespace(value.namespace, value.project_id)
      || !segment(value.id) || !['source', 'note', 'summary', 'insight'].includes(value.layer)
      || typeof value.text !== 'string' || value.title != null && typeof value.title !== 'string') invalid();
  return value;
}

export function validateConversationCommand(command, source) {
  const action = command?.expected?.action, body = command?.body, id = command?.expected?.shareId;
  if (!segment(command?.id) || command.id.length > 64 || !body || Array.isArray(body)) invalid();
  if (action === 'send') {
    if (!source || command.path !== '' || !sameShareSource(body, source) || !sourceRevision(body.source_revision)
        || !segment(body.to_user_id) || !Array.isArray(body.include_citations)
        || body.include_citations.some((index, position) => !ordinal(index) || position > 0 && body.include_citations[position - 1] >= index)
        || Object.keys(body).length !== 6) invalid();
  } else if (action === 'file') {
    if (source || !segment(id) || command.path !== `/${id}/file`
        || !segment(body.project_id) || !revision(body.expected_revision) || Object.keys(body).length !== 2) invalid();
  } else if (action === 'revoke') {
    if (!source || !segment(id) || command.path !== `/${id}/revoke`
        || !revision(body.expected_revision) || Object.keys(body).length !== 1) invalid();
  } else invalid();
  return command;
}

export function conversationCommandShare(value, command, source) {
  const { action, shareId } = command.expected;
  const row = conversationShareRow(value, action === 'file' ? 'inbox' : 'sent', { id: shareId, detail: true });
  if (action !== 'file' && !sameShareSource(row.source, source)) invalid();
  if (action === 'send' && row.user_id !== command.body.to_user_id) invalid();
  if (action !== 'send' && row.revision <= command.body.expected_revision) invalid();
  if (action === 'revoke' && row.state !== 'revoked') invalid();
  if (action === 'file' && row.state !== 'revoked') {
    const target = row.state === 'filed' ? row.archive?.project_id : row.filing?.project_id;
    if (!['filed', 'filing'].includes(row.state) || target !== command.body.project_id
        || row.state === 'filing' && (row.filing.command_key !== command.id
          || row.filing.expected_revision !== command.body.expected_revision)) invalid();
  }
  return row;
}
