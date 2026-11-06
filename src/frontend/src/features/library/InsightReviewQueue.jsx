import { useEffect, useRef, useState } from 'react';
import { DraftConflictView, Icon, StatusDot } from '../../shared/ui';
import { deviceIdentity, isDeviceIdentity } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { libraryApi } from './libraryApi';
import { reviewShortcut } from './reviewShortcuts';
import { reviewInsightProject, writableReviewInsightProject, validateReviewInsight } from './reviewInsightAccess';
import { InsightMergeReview } from './InsightMergeReview';
import { InsightSource } from '../../shared/ui/InsightSource';
import { DraftNavigationDialog, useDraftNavigation } from './DraftNavigation';

const draftConditions = insight => (insight?.conditions || []).join('\n');
export const reviewRelationLabels = { duplicate_of: '可能重复', may_supersede: '可能取代', supplement: '补充依据', differs: '可能矛盾', new: '新认识' };

export function InsightReviewQueue({ projectId, projects, scene, query, detailError, onBack, onChanged, onOpenEvidence, onOpenInsight, onBusy, onNavigationGuard, onReference,
  api = libraryApi, scopeKind = 'personal', capabilities = { edit: true, move: true, merge: true }, supersession, sharedMerge }) {
  const [items, setItems] = useState(null), [loadedDetail, setDetail] = useState(null);
  const [error, setError] = useState(''), [reload, setReload] = useState(0);
  const [busy, setBusy] = useState(false), [editing, setEditing] = useState(false);
  const [mergeBusy, setMergeBusy] = useState(false);
  const [text, setText] = useState(''), [target, setTarget] = useState(projectId);
  const [conditions, setConditions] = useState(''), [deferred, setDeferred] = useState(() => new Set());
  const [results, setResults] = useState({ confirmed: 0, dropped: 0 });
  const pending = useRef(false);
  const [conflict, setConflict] = useState(null), [readingConflict, setReadingConflict] = useState(false);
  const [invalidated, setInvalidated] = useState(false);
  const latest = useRef(null), editor = useRef(null), heading = useRef(null), actions = useRef(null);
  const owner = useRef(deviceIdentity());
  const remaining = items?.filter(row => !deferred.has(row.id));
  const current = remaining?.[0];
  const detail = current && loadedDetail?.insight.id === current.id ? loadedDetail : null;
  const supersessionBlocked = Boolean(supersession?.selecting || supersession?.busy
    || current && supersession?.lockedCandidate === current.id);
  const gate = useRequestScope(`${scopeKind}\0${projectId}\0${scene}\0${query}\0${current?.id || ''}`), scope = gate.scope;
  const dirty = Boolean(editing && detail?.insight && (text !== detail.insight.text || conditions !== draftConditions(detail.insight)));
  latest.current = { scope, dirty, busy: busy || readingConflict || mergeBusy || supersession?.busy, invalidated, detail, text, conflict };
  const guard = useDraftNavigation({ gate, latest, owner, editor });
  const { requestNavigation, navigation, pendingNavigation, finishNavigation } = guard;
  useEffect(() => { onNavigationGuard?.(requestNavigation); return () => onNavigationGuard?.(null); }, [onNavigationGuard, requestNavigation]);
  useEffect(() => { onBusy(busy || editing || readingConflict || mergeBusy); }, [busy, editing, readingConflict, mergeBusy, onBusy]);
  useEffect(() => () => onBusy(false), [onBusy]);
  useEffect(() => {
    const changed = () => {
      gate.invalidate('action'); gate.invalidate('detail'); gate.invalidate('list'); gate.invalidate('conflict');
      latest.current = { ...latest.current, invalidated: true, dirty: false, busy: false };
      guard.clearNavigation(); setInvalidated(true);
      setText(''); setConditions(''); setDeferred(new Set()); setDetail(null); setConflict(null); setBusy(false); setEditing(false); setReadingConflict(false);
    };
    window.addEventListener('chriptmas-user-space', changed); window.addEventListener('chriptmas-device-required', changed);
    return () => {
      pendingNavigation.current = null;
      window.removeEventListener('chriptmas-user-space', changed); window.removeEventListener('chriptmas-device-required', changed);
    };
  }, [gate]);
  useEffect(() => {
    const controller = new AbortController(), request = gate.issue('list', scope);
    api.list('insight', projectId, { scene, q: query, signal: controller.signal }).then(value => {
      if (gate.isCurrent(request)) setItems(value.items.filter(row => row.state === 'pending'));
    }).catch(reason => { if (gate.isCurrent(request)) setError(reason.message); });
    return () => controller.abort();
  }, [projectId, scene, query, reload, supersession?.refresh]);
  useEffect(() => {
    setDetail(null); setEditing(false); setTarget(projectId); setError(''); setConflict(null);
    if (!current) return;
    const request = gate.issue('detail', scope);
    api.drill(projectId, 'insight', current.id).then(value => {
      validateReviewInsight(value, { id: current.id, project: current.project_id, namespace: current.namespace,
        fallbackProject: projectId });
      if (gate.isCurrent(request) && isDeviceIdentity(owner.current)) {
        setDetail(value); setText(value.insight.text); setConditions(draftConditions(value.insight)); setTarget(reviewInsightProject(value, projectId));
      }
    }).catch(reason => { if (gate.isCurrent(request)) setError(reason.message); });
  }, [projectId, current?.id, reload, supersession?.refresh]);
  useEffect(() => { if (items) heading.current?.focus(); }, [current?.id, Boolean(items)]);
  useEffect(() => {
    const candidateId = supersession?.completed;
    if (!candidateId || !items?.some(row => row.id === candidateId)) return;
    setItems(value => value.filter(row => row.id !== candidateId));
    setResults(value => ({ ...value, confirmed: value.confirmed + 1 }));
  }, [supersession?.completed]);
  async function readConflict(base, actualProject = reviewInsightProject(detail, projectId), namespace = detail?.namespace) {
    if (!base || !actualProject || base.id !== current?.id || invalidated || !isDeviceIdentity(owner.current)) return;
    const request = gate.issue('conflict', scope);
    if (!request) return;
    setReadingConflict(true); setConflict({ base, project: actualProject, namespace, data: null, error: '' });
    try {
      const data = await api.drill(actualProject, 'insight', base.id);
      if (!gate.isCurrent(request) || !isDeviceIdentity(owner.current)) return;
      validateReviewInsight(data, { id: base.id, project: actualProject, namespace, fallbackProject: actualProject });
      setConflict({ base, project: actualProject, namespace, data, error: '' });
    } catch {
      if (gate.isCurrent(request) && isDeviceIdentity(owner.current)) setConflict({ base, project: actualProject, namespace,
        data: null, error: '当前版本未读取 · 重试' });
    } finally { if (gate.isCurrent(request)) setReadingConflict(false); }
  }
  function chooseConflict(server) {
    if (!conflict?.data || busy || readingConflict || invalidated || !gate.isScopeCurrent(scope)
        || !isDeviceIdentity(owner.current) || conflict.data.insight.id !== current?.id) return;
    const data = conflict.data;
    setDetail(data); setConflict(null); setError('');
    if (server) { setText(data.insight.text); setConditions(draftConditions(data.insight)); }
    setEditing(data.insight.state === 'pending' && Boolean(writableReviewInsightProject(data, conflict.project)));
  }
  async function act(action) {
    if (action === 'edit' && !capabilities.edit) return;
    const insight = detail?.insight;
    const actualProject = writableReviewInsightProject(detail, projectId);
    if (pending.current || busy || mergeBusy || supersessionBlocked || readingConflict || conflict || invalidated || !isDeviceIdentity(owner.current)
        || !insight || insight.id !== current?.id || !actualProject || insight.state !== 'pending' || detail.needs_detail_read) return;
    if (action === 'confirm' && editing) return;
    const request = gate.issue('action', scope);
    if (!request) return;
    pending.current = true; setBusy(true); setError('');
    try {
      if (action === 'edit') {
        const saved = await api.edit(actualProject, insight, text, conditions.split('\n').map(value => value.trim()).filter(Boolean), detail);
        const updated = scopeKind === 'shared' ? saved : { ...detail, insight: saved };
        validateReviewInsight(updated, { id: insight.id, project: actualProject,
          namespace: detail.namespace, fallbackProject: actualProject });
        if (gate.isCurrent(request) && isDeviceIdentity(owner.current)) {
          const savedDetail = { ...updated, needs_detail_read: true };
          setDetail(savedDetail); setText(updated.insight.text); setConditions(draftConditions(updated.insight)); setEditing(false);
          latest.current = { ...latest.current, dirty: false, busy: false, conflict: null, detail: savedDetail, text: updated.insight.text };
          onChanged();
          if (pendingNavigation.current) {
            finishNavigation(pendingNavigation.current);
            return;
          }
          try {
            const fresh = await api.drill(actualProject, 'insight', insight.id);
            validateReviewInsight(fresh, { id: insight.id, project: actualProject, namespace: detail.namespace, fallbackProject: actualProject });
            if (fresh.insight.revision < updated.insight.revision) throw new Error('insight_refresh_outdated');
            if (gate.isCurrent(request) && isDeviceIdentity(owner.current)) {
              setDetail(fresh); setText(fresh.insight.text); setConditions(draftConditions(fresh.insight));
            }
          } catch {
            if (gate.isCurrent(request) && isDeviceIdentity(owner.current)) setError('认识已保存 · 依据未读取');
          }
        }
      } else {
        if (action === 'confirm' && target !== actualProject && capabilities.move) await api.confirmTo(actualProject, insight, target);
        else await api.review(actualProject, insight, action, detail);
        if (gate.isCurrent(request)) {
          setItems(value => value.filter(row => row.id !== current.id));
          setResults(value => ({ ...value, [action === 'confirm' ? 'confirmed' : 'dropped']: value[action === 'confirm' ? 'confirmed' : 'dropped'] + 1 }));
          latest.current = { ...latest.current, busy: false };
          onChanged(); finishNavigation(pendingNavigation.current);
        }
      }
    } catch (reason) {
      if (gate.isCurrent(request) && isDeviceIdentity(owner.current)) {
        setError(reason.message);
        if (action === 'edit' && reason.status === 409) await readConflict(insight, actualProject, detail.namespace);
      }
    }
    finally { pending.current = false; if (gate.isCurrent(request)) setBusy(false); }
  }
  const completed = results.confirmed + results.dropped;
  const actualProject = reviewInsightProject(detail, projectId) || projectId;
  const writableProject = detail?.insight?.id === current?.id ? writableReviewInsightProject(detail, projectId) : null;
  const available = projects.filter(project => project.id === actualProject || actualProject === 'inbox'
    || project.id === detail?.insight?.hint?.scope_hint);
  const canAct = !busy && !mergeBusy && !supersessionBlocked && !conflict && !detail?.needs_detail_read
    && detail?.insight?.state === 'pending' && Boolean(writableProject);
  const canDefer = !busy && !editing && !mergeBusy && !supersessionBlocked && !conflict && !readingConflict;
  const evidenceDetail = detail?.needs_detail_read ? null : detail;
  const relation = evidenceDetail?.insight?.hint, related = relation?.target;
  const location = id => id === 'me' ? '所有项目（我）' : `${projects.find(project => project.id === id)?.name || id}${id === actualProject && current?.scene ? ` / ${current.scene}` : ''}`;
  function handleActionKey(event) {
    const action = reviewShortcut({ key: event.key, composing: event.nativeEvent.isComposing || event.keyCode === 229,
      repeat: event.repeat, modified: event.ctrlKey || event.metaKey || event.altKey || event.shiftKey,
      formControl: !event.target.closest('button'), interactiveEnter: true,
      queueFocus: actions.current?.contains(document.activeElement), modalOpen: Boolean(navigation),
      disabled: !canAct || editing || readingConflict || invalidated || pending.current });
    if (!action) return;
    event.preventDefault();
    if (action === 'edit') { if (capabilities.edit) setEditing(true); } else act(action);
  }
  function resetEdit() {
    setText(detail?.insight?.text || ''); setConditions(draftConditions(detail?.insight));
    setEditing(false); setConflict(null); setError('');
  }
  const conflictView = conflict && (conflict.data
    ? <DraftConflictView compact baseDraft={{ text: conflict.base.text, conditions: draftConditions(conflict.base) }} editor={{ text, conditions }}
        server={{ text: conflict.data.insight.text, conditions: draftConditions(conflict.data.insight) }} fields={[["text", "认识"], ["conditions", "适用条件"]]}
        confirmed={!writableReviewInsightProject(conflict.data, conflict.project) || conflict.data.insight.state !== 'pending'} onChoose={chooseConflict} disabled={busy || readingConflict}/>
    : <div className="library-error" role={readingConflict ? 'status' : 'alert'}><span>{readingConflict ? '读取当前版本' : conflict.error}</span>
        {!readingConflict && <button type="button" onClick={() => readConflict(conflict.base, conflict.project, conflict.namespace)}>重新读取</button>}</div>);
  if (invalidated) return <main className="library-main"><p role="status">空间已变化</p></main>;
  return <main className="library-main library-review-queue">
    <header className="library-queue-heading"><button type="button" aria-label="回到列表" onClick={() => requestNavigation(onBack)}><Icon name="chevron-down" className="library-back-icon"/></button><h2 ref={current ? null : heading} tabIndex={-1}>逐条确认</h2><span className="library-meta">{items ? `${completed + (current ? 1 : 0)} / ${completed + items.length}` : '—'}</span></header>
    {detailError}
    {error && <div className="library-error" role="alert"><span>{error}</span><button type="button" disabled={busy || readingConflict || mergeBusy} onClick={() => dirty || conflict ? readConflict(detail.insight) : setReload(value => value + 1)}>重新读取</button></div>}
    {!navigation && conflictView}
    {!items && !error && <p className="library-meta" role="status">读取中</p>}
    {current && <article className="library-queue-card" aria-label="当前待确认认识">
      <div className="library-queue-meta"><StatusDot state="pending"/><span>待确认</span><span className="library-queue-scope">#{projects.find(project => project.id === actualProject)?.name || actualProject}{current.scene ? `/${current.scene}` : ''}</span></div>
      {editing ? <div className="library-edit"><label>认识正文<textarea ref={editor} autoFocus aria-label="编辑待确认认识" value={text} disabled={busy || readingConflict} onChange={event => setText(event.target.value)}/></label>
        <label>适用条件<textarea value={conditions} disabled={busy || readingConflict} onChange={event => setConditions(event.target.value)}/></label></div>
        : <><p ref={heading} tabIndex={-1} className="library-queue-text">{detail?.insight?.text || current.text}</p>
          {detail && <div className="library-queue-conditions"><span>适用条件</span>{detail.insight.conditions.length > 0
            ? <ul className="library-conditions">{detail.insight.conditions.map((value, index) => <li key={index}>{value}</li>)}</ul>
            : <p>未设条件</p>}</div>}</>}
      {!evidenceDetail && !error && <p className="library-meta" role="status">读取依据中</p>}
      {evidenceDetail?.insight?.state === 'pending' && <InsightSource source={detail.insight.source} onReference={onReference
        ? reference => requestNavigation(() => onReference(reference)) : undefined}/>}
      {evidenceDetail?.evidence?.map((evidence, index) => <section className="library-queue-evidence" key={`${evidence.source_id}:${evidence.document_id}:${index}`}>
        <header><Icon name="book" size={14}/><span>{evidence.title}</span><button type="button" disabled={busy || editing || mergeBusy} onClick={() => onOpenEvidence(evidence, detail)}>打开原件<Icon name="open" size={14}/></button></header>
        <blockquote>{evidence.window.pre}<mark>{evidence.window.quote}</mark>{evidence.window.post}</blockquote>
      </section>)}
      {evidenceDetail && !detail.evidence?.length && !detail.insight.source && <p className="library-meta">暂无可核对的原句</p>}
      {related && <div className="library-queue-related"><Icon name={relation.relation} size={16}/><div>
        <strong>{reviewRelationLabels[relation.relation] || '已有认识'}</strong>
        <p>{related.text}</p>
        {related.conditions?.length > 0 && <ul className="library-conditions" aria-label="旧认识适用条件">{related.conditions.map((value, index) => <li key={index}>{value}</li>)}</ul>}
        {onOpenInsight && (scopeKind !== 'shared' || !related.project_id || related.project_id === projectId) && <button type="button" disabled={busy || editing || mergeBusy} onClick={() => requestNavigation(() => onOpenInsight(related, related.project_id || actualProject))}>打开旧认识</button>}
      </div></div>}
      {capabilities.move && available.some(project => project.id !== actualProject) && <label className="library-queue-target">确认到<select aria-label="确认到" value={target} disabled={!canAct || editing} onChange={event => setTarget(event.target.value)}>
        {!available.some(project => project.id === actualProject) && <option value={actualProject}>当前项目</option>}
        {available.map(project => <option key={project.id} value={project.id}>{project.id === 'me' ? '我' : `#${project.name}`}</option>)}
      </select></label>}
      {detail && <p className="library-meta library-queue-destination">确认后在 {location(capabilities.move ? target : actualProject)} 生效</p>}
      <div ref={actions} role="group" aria-label="当前认识操作" onKeyDown={handleActionKey} className="library-actions library-queue-actions">
        <button type="button" disabled={!canDefer} onClick={() => setDeferred(previous => new Set([...previous, current.id]))}>暂缓</button>
        <button type="button" title="操作区内按 D 丢弃" aria-keyshortcuts="D" disabled={!canAct || editing} onClick={() => act('drop')}><Icon name="drop" size={16}/>丢弃</button>
        {capabilities.edit && (editing ? <><button type="button" disabled={busy || readingConflict || Boolean(conflict) || !text.trim()} onClick={() => act('edit')}>保存</button><button type="button" disabled={busy || readingConflict} onClick={resetEdit}>取消</button></> : <button type="button" title="操作区内按 E 编辑" aria-keyshortcuts="E" disabled={!canAct} onClick={() => setEditing(true)}><Icon name="code" size={16}/>编辑</button>)}
        {supersession?.onOpen && !supersession.lockedCandidate && detail?.insight?.state === 'pending' && writableProject
          && <button type="button" disabled={!canAct || editing || readingConflict || Boolean(conflict)}
            onClick={() => { if (!pending.current && canAct && isDeviceIdentity(owner.current)) supersession.onOpen(detail); }}>
            <Icon name="supersedes" size={16}/>取代已有认识</button>}
        <button type="button" className="library-primary" disabled={!canAct || editing} onClick={() => act('confirm')}><Icon name="check" size={16}/>确认</button>
      </div>
    </article>}
    {(capabilities.merge || sharedMerge) && <InsightMergeReview key={sharedMerge?.storage || projectId}
      projectId={projectId} detail={evidenceDetail} readonly={editing || Boolean(conflict) || supersessionBlocked} shared={sharedMerge}
      onBusy={setMergeBusy} onChanged={() => {
        const reload = () => { setReload(value => value + 1); onChanged(); };
        if (latest.current?.dirty || latest.current?.conflict) requestNavigation(reload); else reload();
      }}
      onOpenTarget={onOpenInsight ? (row, project) => requestNavigation(() => onOpenInsight(row, project)) : undefined}/>}
    {remaining?.length > 1 && <div className="library-queue-next"><StatusDot state="pending"/><span>{remaining[1].text}</span></div>}
    {remaining?.length === 0 && <div className="library-queue-finished"><p>{items.length ? `${items.length} 条暂缓` : completed ? `${results.confirmed} 确认 · ${results.dropped} 丢弃` : '没有待确认的认识'}</p>
      {items.length > 0 && <button type="button" onClick={() => setDeferred(new Set())}>继续审核</button>}
      <button type="button" onClick={() => requestNavigation(onBack)}>回到列表</button></div>}
    <DraftNavigationDialog guard={guard} busy={busy || readingConflict || mergeBusy || supersession?.busy} dirty={dirty}
      canSave={!conflict && (!dirty || canAct && Boolean(text.trim()))} conflict={conflictView} error={error}
      onSave={() => act('edit')} onDiscard={() => {
          resetEdit();
          latest.current = { ...latest.current, dirty: false, conflict: null };
          finishNavigation(pendingNavigation.current);
      }}/>
  </main>;
}
