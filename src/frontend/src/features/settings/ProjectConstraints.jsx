import { useEffect, useRef, useState } from 'react';
import { DraftConflictView, Row, Switch } from '../../shared/ui';
import { recognitionApi } from '../../shared/api/recognitionApi';
import { userStorageKey } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';

function createDraft(content = '') {
  return { base: { id: `constraint-${crypto.randomUUID()}`, revision: 0, enabled: true }, content };
}

export function ProjectConstraints({ projectId, onChanged = () => {}, api = recognitionApi }) {
  const [identity, setIdentity] = useState(0);
  const userSpace = userStorageKey(projectId);
  const gate = useRequestScope(`${userSpace}:${identity}`), scope = gate.scope;
  const [list, setList] = useState({ scope, items: [] }), [editor, setEditor] = useState(null);
  const [newDraft, setNewDraft] = useState(createDraft), [conflict, setConflict] = useState(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [reading, setReading] = useState(false), [readError, setReadError] = useState(null);
  const currentEditor = useRef(editor), currentNewDraft = useRef(newDraft);
  currentEditor.current = editor; currentNewDraft.current = newDraft;
  const current = request => userStorageKey(projectId) === userSpace && gate.isCurrent(request);
  const items = list.scope === scope ? list.items : [];
  function updateEditor(value) { currentEditor.current = value; setEditor(value); }
  function updateNewDraft(value) { currentNewDraft.current = value; setNewDraft(value); }
  function matches(snapshot) {
    return snapshot?.session ? currentEditor.current?.session === snapshot.session
      : snapshot?.creating && !currentEditor.current && currentNewDraft.current.base.id === snapshot.creating.base.id;
  }
  useEffect(() => {
    const changed = () => { gate.invalidate('read'); gate.invalidate('write'); setIdentity(value => value + 1); };
    window.addEventListener('chriptmas-user-space', changed);
    window.addEventListener('chriptmas-device-required', changed);
    return () => {
      window.removeEventListener('chriptmas-user-space', changed);
      window.removeEventListener('chriptmas-device-required', changed);
    };
  }, [gate]);
  useEffect(() => {
    setList({ scope, items: [] }); updateEditor(null); updateNewDraft(createDraft());
    setConflict(null); setBusy(false); setError('');
    load();
    return () => { gate.invalidate('read'); gate.invalidate('write'); };
  }, [scope, api]);
  async function load(snapshot = null) {
    const request = gate.issue('read', scope);
    if (!request) return;
    setReading(true); setReadError(null);
    if (matches(snapshot)) setConflict({ ...snapshot, server: null, error: '' });
    try {
      const result = await api.loadConstraints({ projectId });
      if (!current(request)) return;
      const rows = result.items || [];
      setList({ scope, items: rows });
      if (matches(snapshot)) {
        const server = rows.find(row => row.id === snapshot.base.id);
        if (!server) throw new Error('读取未完成');
        setConflict({ ...snapshot, server, error: '' });
      } else if (snapshot && !snapshot.session && !snapshot.creating) setError('内容已变化');
    } catch {
      if (!current(request)) return;
      if (matches(snapshot))
        setConflict({ ...snapshot, server: null, error: '读取未完成' });
      else if (!snapshot?.session && !snapshot?.creating) setReadError(snapshot || true);
    } finally { if (current(request)) setReading(false); }
  }
  function closeEditor() {
    gate.invalidate('read'); updateEditor(null); setConflict(null); setReading(false); setReadError(null); setError('');
  }
  function openEditor(row) {
    closeEditor(); updateEditor({ base: row, content: row.content, session: {} });
  }
  function changeEditor(content, session) {
    const latest = currentEditor.current;
    if (latest?.session === session) updateEditor({ ...latest, content });
  }
  async function save(row, enabled, text = row.content, editing = null, creating = null) {
    if (busy || reading) return;
    const request = gate.issue('write', scope);
    if (!request) return;
    setBusy(true); setError('');
    try {
      const result = await api.saveConstraint({ projectId, constraintId: row.id, expectedRevision: row.revision,
        content: text, enabled, validFrom: row.valid_from || null, validUntil: row.valid_until || null });
      if (!current(request)) return;
      setList(old => ({ scope, items: [...old.items.filter(item => item.id !== result.id), result] }));
      const latest = currentEditor.current;
      // 只结算本次提交的编辑会话，等待期间的新输入继续使用新修订。
      if (editing && latest?.session === editing.session)
        updateEditor(latest === editing ? null : { ...latest, base: result });
      else if (latest?.base.id === row.id) updateEditor({ ...latest, base: result });
      if (creating && currentNewDraft.current.base.id === creating.base.id)
        updateNewDraft(createDraft(currentNewDraft.current === creating ? '' : currentNewDraft.current.content));
      onChanged();
    } catch (failure) {
      if (!current(request) || editing && !matches(editing) || creating && !matches({ creating })) return;
      if (failure.status === 409) load({ base: row, session: editing?.session, creating });
      else setError('操作未完成 · 重试');
    } finally { if (current(request)) setBusy(false); }
  }
  function choose(useServer) {
    if (!conflict?.server || !matches(conflict)) return;
    if (conflict.creating) updateNewDraft(useServer ? createDraft() : { ...currentNewDraft.current, base: conflict.server });
    else {
      const latest = currentEditor.current;
      updateEditor({ ...latest, base: conflict.server, content: useServer ? conflict.server.content : latest.content });
    }
    setConflict(null); setError('');
  }
  const conflictView = conflict?.server ? <DraftConflictView compact baseDraft={conflict.base} editor={editor || newDraft} server={conflict.server}
    fields={[["content", "约束正文"]]} onChoose={choose} disabled={busy || reading}/>
    : conflict && <p role={reading ? 'status' : 'alert'}>{reading ? '读取中' : conflict.error}
      {conflict.error && <button type="button" disabled={busy || reading} onClick={() => load(conflict)}>重试</button>}</p>;
  if (list.scope !== scope) return null;
  return <section className="library-tools">
    {readError && <p role="alert">读取未完成 · <button type="button" disabled={busy || reading}
      onClick={() => load(readError === true ? null : readError)}>重试</button></p>}
    {error && <p role="alert">{error}</p>}
    <div className="settings-constraints-head">约束 <span>{items.filter(row => row.enabled).length}/{items.length}</span></div>
    {editor && <form className="library-edit" onSubmit={event => { event.preventDefault(); if (!conflict) save(editor.base, editor.base.enabled, editor.content, editor); }}>
      {conflictView}
      <label>约束正文<textarea aria-label="约束正文" value={editor.content} onChange={event => changeEditor(event.target.value, editor.session)}/></label>
      <button type="submit" disabled={busy || reading || Boolean(conflict) || !editor.content.trim()}>保存</button><button type="button" onClick={closeEditor}>取消</button>
    </form>}
    {items.map(row => <Row key={row.id} title={row.content} meta={row.valid_until ? String(row.valid_until).slice(0, 10) : '—'}
      onOpen={() => openEditor(row)}
      trailing={<Switch label={row.content} checked={row.enabled} disabled={busy || reading || conflict?.base.id === row.id} onChange={enabled => save(row, enabled)}/>}/>)}
    <form onSubmit={event => { event.preventDefault(); if (newDraft.content.trim() && !editor && !conflict) save(newDraft.base, newDraft.base.enabled, newDraft.content, null, newDraft); }}>
      {!editor && <>{conflictView}<label>新约束<input aria-label="新约束" value={newDraft.content} onChange={event => updateNewDraft({ ...newDraft, content: event.target.value })} onKeyDown={event => { if (event.key === 'Enter' && (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229)) event.preventDefault(); }}/></label></>}
    </form>
  </section>;
}
