export function beginCredentialDraft(scope, identity, fields) {
  return { scope, identity, currentIdentity: identity, fields: { ...fields }, edited: {}, version: 0, stale: false };
}

export function receiveCredentialDraft(draft, scope, identity, fields) {
  if (draft.scope !== scope) return beginCredentialDraft(scope, identity, fields);
  const dirty = Object.values(draft.edited).some(Boolean);
  return { ...draft, identity: dirty ? draft.identity : identity, currentIdentity: identity,
    stale: dirty && draft.identity !== identity,
    fields: Object.fromEntries(Object.entries(fields).map(([name, value]) => [name, draft.edited[name] ? draft.fields[name] : value])) };
}

export function editCredentialDraft(draft, name, value) {
  if (!Object.hasOwn(draft.fields, name)) throw new Error('credential_field_invalid');
  return { ...draft, fields: { ...draft.fields, [name]: value }, edited: { ...draft.edited, [name]: true }, version: draft.version + 1 };
}

export function reviewCredentialDraft(draft) {
  return { ...draft, identity: draft.currentIdentity, stale: false, version: draft.version + 1 };
}

export function submitCredentialDraft(draft) {
  if (draft.stale) throw Object.assign(new Error('credential_configuration_changed'), { status: 409 });
  return { scope: draft.scope, version: draft.version, identity: draft.identity, fields: { ...draft.fields } };
}

export function completeCredentialDraft(draft, submitted, identity, fields) {
  if (draft.scope !== submitted.scope) return draft;
  // 只清除本次已确认保存的输入；等待期间的新编辑仍属于下一份草稿。
  if (draft.version !== submitted.version) return receiveCredentialDraft(draft, draft.scope, identity, fields);
  return { ...beginCredentialDraft(draft.scope, identity, fields), version: draft.version + 1 };
}
