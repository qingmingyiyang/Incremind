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

export function InsightReviewQueue({ projectId, projects, scene, query, detailError, onBack, onChanged, onOpenEvidence, onOpenInsight, onBusy, onNavigationGuard, onReference,
  api = libraryApi, scopeKind = 'personal', capabilities = { edit: true, move: true, merge: true }, supersession, sharedMerge }) {
  const [items, setItems] = useState(null), [detail, setDetail] = useState(null);
  const [error, setError] = useState(''), [reload, setReload] = useState(0);
  const [busy, setBusy] = useState(false), [editing, setEditing] = useState(false);
  const [mergeBusy, setMergeBusy] = useState(false);
  const [text, setText] = useState(''), [target, setTarget] = useState(projectId);
  const [results, setResults] = useState({ confirmed: 0, dropped: 0 });
  const pending = useRef(false);
  const [conflict, setConflict] = useState(null), [readingConflict, setReadingConflict] = useState(false);
  const [invalidated, setInvalidated] = useState(false);
  const latest = useRef(null), editor = useRef(null), queue = useRef(null);
  const owner = useRef(deviceIdentity());
  const current = items?.[0];
  const supersessionBlocked = Boolean(supersession?.selecting || supersession?.busy
    || current && supersession?.lockedCandidate === current.id);
  const gate = useRequestScope(`${scopeKind}\0${projectId}\0${scene}\0${query}\0${current?.id || ''}`), scope = gate.scope;
  const dirty = Boolean(editing && detail?.insight && text !== detail.insight.text);
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
      setText(''); setDetail(null); setConflict(null); setBusy(false); setEditing(false); setReadingConflict(false);
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
        setDetail(value); setText(value.insight.text); setTarget(reviewInsightProject(value, projectId));
      }
    }).catch(reason => { if (gate.isCurrent(request)) setError(reason.message); });
  }, [projectId, current?.id, reload, supersession?.refresh]);
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
    if (server) setText(data.insight.text);
    setEditing(data.insight.state === 'pending' && Boolean(writableReviewInsightProject(data, conflict.project)));
  }
  async function act(action) {
    if (action === 'edit' && !capabilities.edit) return;
    const insight = detail?.insight;
    const actualProject = writableReviewInsightProject(detail, projectId);
    if (pending.current || busy || mergeBusy || supersessionBlocked || readingConflict || conflict || invalidated || !isDeviceIdentity(owner.current)
        || !insight || insight.id !== current?.id || !actualProject || insight.state !== 'pending') return;
    if (action === 'confirm' && editing) return;
    const request = gate.issue('action', scope);
    if (!request) return;
    pending.current = true; setBusy(true); setError('');
    try {
      if (action === 'edit') {
        const saved = await api.edit(actualProject, insight, text, insight.conditions || []);
        validateReviewInsight({ ...detail, insight: saved }, { id: insight.id, project: actualProject,
          namespace: detail.namespace, fallbackProject: actualProject });
        if (gate.isCurrent(request) && isDeviceIdentity(owner.current)) {
          setDetail(value => ({ ...value, insight: saved })); setText(saved.text); setEditing(false);
          latest.current = { ...latest.current, dirty: false, busy: false, conflict: null, detail: { ...detail, insight: saved }, text: saved.text };
          onChanged(); finishNavigation(pendingNavigation.current);
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
  const canAct = !busy && !mergeBusy && !supersessionBlocked && detail?.insight?.state === 'pending' && Boolean(writableProject);
  useEffect(() => {
    const keydown = event => {
      const element = event.target instanceof Element ? event.target : null;
      const action = reviewShortcut({ key: event.key, composing: event.isComposing || event.keyCode === 229,
        repeat: event.repeat, modified: event.ctrlKey || event.metaKey || event.altKey || event.shiftKey,
        formControl: Boolean(element?.closest('input,textarea,select,[contenteditable]:not([contenteditable="false"]),[role="textbox"],[role="combobox"]')),
        interactiveEnter: Boolean(element?.closest('button,a,summary,[role="button"]')),
        queueFocus: document.activeElement === document.body || queue.current?.contains(document.activeElement),
        modalOpen: Boolean(document.querySelector('dialog[open],[role="dialog"][aria-modal="true"]')),
        disabled: !canAct || editing || readingConflict || Boolean(navigation || conflict) || invalidated || pending.current });
      if (!action) return;
      event.preventDefault();
      if (action === 'edit') { if (capabilities.edit) setEditing(true); } else act(action);
    };
    window.addEventListener('keydown', keydown);
    return () => window.removeEventListener('keydown', keydown);
  });
  const conflictView = conflict && (conflict.data
    ? <DraftConflictView compact baseDraft={{ text: conflict.base.text }} editor={{ text }} server={{ text: conflict.data.insight.text }} fields={[["text", "认识"]]}
        confirmed={!writableReviewInsightProject(conflict.data, conflict.project) || conflict.data.insight.state !== 'pending'} onChoose={chooseConflict} disabled={busy || readingConflict}/>
    : <div className="library-error" role={readingConflict ? 'status' : 'alert'}><span>{readingConflict ? '读取当前版本' : conflict.error}</span>
        {!readingConflict && <button type="button" onClick={() => readConflict(conflict.base, conflict.project, conflict.namespace)}>重新读取</button>}</div>);
  if (invalidated) return <main className="library-main"><p role="status">空间已变化</p></main>;
  return <main ref={queue} className="library-main library-review-queue">
    <header className="library-queue-heading"><button type="button" aria-label="回到列表" onClick={() => requestNavigation(onBack)}><Icon name="chevron-down" className="library-back-icon"/></button><h2>逐条确认</h2><span className="library-meta">{items ? `${completed + (current ? 1 : 0)} / ${completed + items.length}` : '—'}</span></header>
    {detailError}
    {error && <div className="library-error" role="alert"><span>{error}</span><button type="button" disabled={busy || readingConflict || mergeBusy} onClick={() => dirty || conflict ? readConflict(detail.insight) : setReload(value => value + 1)}>重新读取</button></div>}
    {!navigation && conflictView}
    {!items && !error && <p className="library-meta" role="status">读取中</p>}
    {current && <article className="library-queue-card" aria-label="当前待确认认识">
      <div className="library-queue-meta"><StatusDot state="pending"/><span>待确认</span><span className="library-queue-scope">#{projects.find(project => project.id === actualProject)?.name || actualProject}{current.scene ? `/${current.scene}` : ''}</span></div>
      {editing ? <label className="library-edit">认识正文<textarea ref={editor} autoFocus aria-label="编辑待确认认识" value={text} disabled={busy || readingConflict} onChange={event => setText(event.target.value)}/></label> : <p className="library-queue-text">{detail?.insight?.text || current.text}</p>}
      {!detail && !error && <p className="library-meta" role="status">读取依据中</p>}
      {detail?.insight?.state === 'pending' && <InsightSource source={detail.insight.source} onReference={onReference
        ? reference => requestNavigation(() => onReference(reference)) : undefined}/>}
      {detail?.evidence?.map((evidence, index) => <section className="library-queue-evidence" key={`${evidence.source_id}:${evidence.document_id}:${index}`}>
        <header><Icon name="book" size={14}/><span>{evidence.title}</span><button type="button" disabled={busy || editing || mergeBusy} onClick={() => onOpenEvidence(evidence, detail)}>打开原件<Icon name="open" size={14}/></button></header>
        <blockquote>{evidence.window.pre}<mark>{evidence.window.quote}</mark>{evidence.window.post}</blockquote>
      </section>)}
      {detail?.insight?.hint?.target && <div className="library-queue-related"><Icon name={detail.insight.hint.relation} size={16}/><span>{detail.insight.hint.target.text}</span></div>}
      {capabilities.move && <label className="library-queue-target">确认到<select aria-label="确认到" value={target} disabled={!canAct || editing} onChange={event => setTarget(event.target.value)}>
        {!available.some(project => project.id === actualProject) && <option value={actualProject}>当前项目</option>}
        {available.map(project => <option key={project.id} value={project.id}>{project.id === 'me' ? '我' : `#${project.name}`}</option>)}
      </select></label>}
      <div className="library-actions library-queue-actions"><button type="button" title="快捷键 D" aria-keyshortcuts="D" disabled={!canAct || editing} onClick={() => act('drop')}><Icon name="drop" size={16}/>丢弃</button>
        {capabilities.edit && (editing ? <><button type="button" disabled={busy || readingConflict || Boolean(conflict) || !text.trim()} onClick={() => act('edit')}>保存</button><button type="button" disabled={busy || readingConflict} onClick={() => { setText(detail.insight.text); setEditing(false); setConflict(null); setError(''); }}>取消</button></> : <button type="button" title="快捷键 E" aria-keyshortcuts="E" disabled={!canAct} onClick={() => setEditing(true)}><Icon name="code" size={16}/>编辑</button>)}
        {supersession?.onOpen && !supersession.lockedCandidate && detail?.insight?.state === 'pending' && writableProject
          && <button type="button" disabled={!canAct || editing || readingConflict || Boolean(conflict)}
            onClick={() => { if (!pending.current && canAct && isDeviceIdentity(owner.current)) supersession.onOpen(detail); }}>
            <Icon name="supersedes" size={16}/>取代已有认识</button>}
        <button type="button" className="library-primary" title="快捷键 Enter" aria-keyshortcuts="Enter" disabled={!canAct || editing} onClick={() => act('confirm')}><Icon name="check" size={16}/>确认</button>
      </div>
    </article>}
    {(capabilities.merge || sharedMerge) && <InsightMergeReview key={sharedMerge?.storage || projectId}
      projectId={projectId} detail={detail} readonly={editing || Boolean(conflict) || supersessionBlocked} shared={sharedMerge}
      onBusy={setMergeBusy} onChanged={() => {
        const reload = () => { setReload(value => value + 1); onChanged(); };
        if (latest.current?.dirty || latest.current?.conflict) requestNavigation(reload); else reload();
      }}
      onOpenTarget={onOpenInsight ? (row, project) => requestNavigation(() => onOpenInsight(row, project)) : undefined}/>}
    {items?.length > 1 && <div className="library-queue-next"><StatusDot state="pending"/><span>{items[1].text}</span></div>}
    {items?.length === 0 && <div className="library-queue-finished"><Icon name="check" size={32}/><p>{results.confirmed} 确认 · {results.dropped} 丢弃</p><button type="button" onClick={() => requestNavigation(onBack)}>回到列表</button></div>}
    <DraftNavigationDialog guard={guard} busy={busy || readingConflict || mergeBusy || supersession?.busy} dirty={dirty}
      canSave={!conflict && (!dirty || canAct && Boolean(text.trim()))} conflict={conflictView} error={error}
      onSave={() => act('edit')} onDiscard={() => {
          setText(detail?.insight?.text || ''); setEditing(false); setConflict(null); setError('');
          latest.current = { ...latest.current, dirty: false, conflict: null };
          finishNavigation(pendingNavigation.current);
      }}/>
  </main>;
}
