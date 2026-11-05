import { useEffect, useRef, useState } from 'react';
import { DraftConflictView } from '../../shared/ui';
import { deviceIdentity, isDeviceIdentity } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { libraryApi } from './libraryApi';
import { validateReviewInsight } from './reviewInsightAccess';
import { DraftNavigationDialog, useDraftNavigation } from './DraftNavigation';

const insightDraft = value => ({ text: value.text, conditions: (value.conditions || []).join('\n') });

export function InsightEditor({ projectId, insight, disabled, onBusy, onAdopt, onSaved, onCancel,
  api = libraryApi, namespace, onNavigationGuard }) {
  const [base, setBase] = useState(insight), [text, setText] = useState(insight.text);
  const [conditions, setConditions] = useState((insight.conditions || []).join('\n'));
  const [conflict, setConflict] = useState(null), [error, setError] = useState('');
  const [saving, setSaving] = useState(false), [reading, setReading] = useState(false), [invalidated, setInvalidated] = useState(false);
  const gate = useRequestScope(`${namespace || 'personal'}:${projectId}:${insight.id}`), scope = gate.scope;
  const owner = useRef(deviceIdentity()), editor = useRef(null), latest = useRef(null);
  const dirty = text !== base.text || conditions !== (base.conditions || []).join('\n');
  latest.current = { scope, dirty, busy: saving || reading, conflict, invalidated };
  const guard = useDraftNavigation({ gate, latest, owner, editor });
  useEffect(() => { onNavigationGuard?.(guard.requestNavigation); return () => onNavigationGuard?.(null); }, [onNavigationGuard, guard.requestNavigation]);
  useEffect(() => { onBusy(saving || reading); }, [saving, reading, onBusy]);
  useEffect(() => () => onBusy(false), [onBusy]);
  useEffect(() => {
    const changed = () => {
      latest.current.invalidated = true; guard.clearNavigation(); gate.invalidate('save'); gate.invalidate('conflict');
      setText(''); setConditions(''); setConflict(null); setError(''); setInvalidated(true);
    };
    window.addEventListener('chriptmas-user-space', changed); window.addEventListener('chriptmas-device-required', changed);
    return () => { window.removeEventListener('chriptmas-user-space', changed); window.removeEventListener('chriptmas-device-required', changed); };
  }, [gate]);
  async function readConflict(snapshot) {
    const request = gate.issue('conflict', scope);
    if (!request || !isDeviceIdentity(owner.current)) return;
    setReading(true);
    setConflict(current => current?.base === snapshot.base ? { ...current, error: '' } : current);
    try {
      const data = await api.drill(projectId, 'insight', snapshot.base.id);
      validateReviewInsight(data, { id: snapshot.base.id, project: projectId, namespace, fallbackProject: projectId });
      if (!gate.isCurrent(request) || !isDeviceIdentity(owner.current)) return;
      setConflict(current => current?.base === snapshot.base ? { ...current, data, server: data.insight, error: '' } : current);
    } catch {
      if (gate.isCurrent(request) && isDeviceIdentity(owner.current)) setConflict(current => current?.base === snapshot.base
        ? { ...current, error: '读取未完成 · 重试' } : current);
    } finally { if (gate.isCurrent(request)) setReading(false); }
  }
  async function save(event) {
    event?.preventDefault();
    if (disabled || saving || reading || conflict || invalidated || !isDeviceIdentity(owner.current) || !['pending', 'active'].includes(base.state)) return;
    const request = gate.issue('save', scope);
    if (!request) return;
    setSaving(true); setError('');
    try {
      const result = await api.edit(projectId, base, text, conditions.split('\n').map(value => value.trim()).filter(Boolean));
      if (gate.isCurrent(request) && isDeviceIdentity(owner.current)) {
        latest.current = { ...latest.current, dirty: false, busy: false, conflict: null };
        onSaved(result); guard.finishNavigation(guard.pendingNavigation.current);
      }
    } catch (failure) {
      if (!gate.isCurrent(request) || !isDeviceIdentity(owner.current)) return;
      if (failure.status === 409) {
        const snapshot = { base, server: null, data: null, error: '' };
        setConflict(snapshot); await readConflict(snapshot);
      } else setError(failure.message);
    } finally { if (gate.isCurrent(request)) setSaving(false); }
  }
  function choose(server) {
    const next = conflict.server;
    if (server) { setText(next.text); setConditions((next.conditions || []).join('\n')); }
    setBase(next); setConflict(null); setError(''); onAdopt(conflict.data);
    if (!conflict.data.can_edit_insight || conflict.data.insight_project_id !== projectId) onCancel();
  }
  const conflictView = <>
    {conflict?.server && <DraftConflictView compact baseDraft={insightDraft(conflict.base)} editor={{ text, conditions }}
      server={insightDraft(conflict.server)} fields={[["text", "认识"], ["conditions", "条件"]]}
      confirmed={!conflict.data.can_edit_insight || conflict.data.insight_project_id !== projectId}
      onChoose={choose} disabled={disabled || saving || reading}/>}
    {conflict && !conflict.server && <div className="library-error" role={reading ? 'status' : 'alert'}>
      <span>{reading ? '读取中' : conflict.error}</span>{conflict.error && <button type="button" disabled={disabled || reading}
        onClick={() => readConflict(conflict)}>重试</button>}
    </div>}
  </>;
  if (invalidated) return <p role="status">空间已变化</p>;
  return <>
    <form className="library-edit" onSubmit={save}>
      {error && <div className="library-error" role="alert"><span>{error}</span></div>}
      {!guard.navigation && conflictView}
      <label>认识<textarea ref={editor} aria-label="认识正文" disabled={disabled || saving} value={text} onChange={event => setText(event.target.value)}/></label>
      <label>条件<textarea aria-label="适用条件" disabled={disabled || saving} value={conditions} onChange={event => setConditions(event.target.value)}/></label>
      <button type="submit" disabled={disabled || saving || reading || Boolean(conflict) || !['pending', 'active'].includes(base.state)}>保存</button>
      <button type="button" onClick={() => guard.requestNavigation(onCancel)}>取消</button>
    </form>
    <DraftNavigationDialog guard={guard} busy={saving || reading} dirty={dirty} canSave={!disabled && !conflict && Boolean(text.trim())}
      conflict={conflictView} error={error} onSave={save} onDiscard={() => {
        latest.current = { ...latest.current, dirty: false, conflict: null };
        guard.finishNavigation(guard.pendingNavigation.current);
      }}/>
  </>;
}
