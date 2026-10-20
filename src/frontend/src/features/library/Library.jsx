import { ConsolidationSuggestions } from './ConsolidationSuggestions';
import { InsightLinks } from './InsightLinks';
import { useEffect, useRef, useState } from 'react';
import { LayerTabs, FilterBar, Row, Breadcrumb, FocusPanel, Icon, MarkdownBody, CandidateHint, DraftConflictView } from '../../shared/ui';
import { sendSignal, signalEpoch } from '../../shared/signalsApi';
import { libraryApi } from './libraryApi';
import { SignalReviews, useSignalReviews } from './SignalReviews';
import { Gaps, useGaps } from './Gaps';
import { InboxFiling } from './InboxFiling';
import { DocumentPlacement } from './DocumentPlacement';
import { InboxProjectSuggestion } from './InboxProjectSuggestion';
import { MemoPanel, InsightMaintenance } from './LibraryTools';
import { archiveDocument, restoreDocument, loadArchivedDocuments } from '../rebuild/libraryOverviewApi';
import { recognitionApi } from '../../shared/api/recognitionApi';
import { ProductFileLink } from '../../shared/ui/ProductFileLink';
import { DocumentMarkdownEditor } from '../../shared/ui/DocumentMarkdownEditor';
import { SkillExportPanel } from './SkillExportPanel';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { formatLocalDate } from '../../shared/lib/time';
import { workbenchApi } from '../workbench/workbenchApi';
import './Library.css';

const layers = ['insight', 'summary', 'note', 'source'];
const labels = { insight: '认识', summary: '摘要', note: '整理稿', source: '原件' };
const dots = { pending: 'pending', active: 'done', stale: 'unverified', forgotten: 'forgotten' };
const insightDot = row => row.state === 'forgotten' ? row.recall_by === 'auto' ? 'forgotten' : 'forgotten-user'
  : row.recall_state === 'cooled' ? 'cooled' : dots[row.state];
const kinds = { text: '文字', link: '链接', file: '文件', audio: '录音', image: '图片', video: '视频', other: '原件' };
import { OriginalPrivacy } from './OriginalPrivacy';

function OriginalFiles({ projectId, original, scopeKey }) {
  const gate = useRequestScope(scopeKey), scope = gate.scope;
  const [reading, setReading] = useState(null), [retry, setRetry] = useState(0);
  const uploadedImage = original.kind === 'image' && Boolean(original.download_url);
  useEffect(() => {
    if (!uploadedImage) return;
    const token = gate.issue('images', scope), controller = new AbortController();
    setReading({ scope, loading: true });
    workbenchApi.images(projectId, original.id, { signal: controller.signal })
      .then(value => { if (gate.isCurrent(token)) setReading({ scope, images: value.images }); })
      .catch(() => { if (gate.isCurrent(token)) setReading({ scope, error: true }); });
    return () => { controller.abort(); gate.invalidate('images'); };
  }, [projectId, original.id, uploadedImage, scope, retry]);
  if (!uploadedImage) return (original.url || original.download_url) &&
    <ProductFileLink href={original.url || original.download_url} name={original.title} scopeKey={scopeKey}>打开原件</ProductFileLink>;
  const current = gate.isScopeCurrent(reading?.scope) ? reading : null;
  return <div className="library-original">
    {!current || current.loading ? <span role="status">读取中</span> : current.error ?
      <div className="library-error" role="alert"><span>读取未完成</span><button type="button" onClick={event => {
        event.currentTarget.closest('.ui-focus-panel')?.querySelector('.ui-focus-close')?.focus();
        setRetry(value => value + 1);
      }}>重试</button></div> :
      <ol aria-label="图片原件">{current.images.map(image => <li key={image.ordinal} value={image.ordinal}>
        <ProductFileLink href={image.url} name={image.name} scopeKey={`${scopeKey}:${image.ordinal}`}>{image.name}</ProductFileLink>
      </li>)}</ol>}
  </div>;
}

function OriginalText({ projectId, original }) {
  const [expanded, setExpanded] = useState(!original.window), [full, setFull] = useState(null), [error, setError] = useState(''), [retry, setRetry] = useState(0);
  useEffect(() => {
    if (!expanded) return;
    const controller = new AbortController();
    setFull(null); setError('');
    libraryApi.sourceText(projectId, original.id, { signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) setFull(result); })
      .catch(() => { if (!controller.signal.aborted) setError('读取未完成 · 重试'); });
    return () => controller.abort();
  }, [projectId, original.id, expanded, retry]);
  return <>
    {expanded ? full && <p className="library-original">{full.text}</p> : <p className="library-original">{original.window.pre}<mark>{original.window.quote}</mark>{original.window.post}</p>}
    {!expanded && <div className="library-actions"><button type="button" onClick={() => setExpanded(true)}>展开全文</button></div>}
    {error && <div className="library-error" role="alert"><span>{error}</span><button type="button" onClick={() => setRetry(value => value + 1)}>重试</button></div>}
  </>;
}

const date = value => value ? formatLocalDate(value) : '';

const insightDraft = value => ({ text: value.text, conditions: (value.conditions || []).join('\n') });

function InsightEditor({ projectId, insight, disabled, onBusy, onAdopt, onSaved, onCancel }) {
  const [base, setBase] = useState(insight), [text, setText] = useState(insight.text);
  const [conditions, setConditions] = useState((insight.conditions || []).join('\n'));
  const [conflict, setConflict] = useState(null), [error, setError] = useState('');
  const [saving, setSaving] = useState(false), [reading, setReading] = useState(false);
  const gate = useRequestScope(`${projectId}:${insight.id}`), scope = gate.scope;
  useEffect(() => { onBusy(saving || reading); }, [saving, reading, onBusy]);
  useEffect(() => () => onBusy(false), [onBusy]);
  async function readConflict(snapshot) {
    const request = gate.issue('conflict', scope);
    if (!request) return;
    setReading(true);
    setConflict(current => current?.base === snapshot.base ? { ...current, error: '' } : current);
    try {
      const data = await libraryApi.drill(projectId, 'insight', snapshot.base.id);
      if (!gate.isCurrent(request)) return;
      if (!data.insight || typeof data.insight.id !== 'string' || !data.insight.id
          || typeof data.insight.text !== 'string' || !Number.isInteger(data.insight.revision) || data.insight.revision < 1
          || !Array.isArray(data.insight.conditions) || data.insight.conditions.some(value => typeof value !== 'string'))
        throw new Error('读取未完成 · 重试');
      setConflict(current => current?.base === snapshot.base ? { ...current, data, server: data.insight, error: '' } : current);
    } catch {
      if (gate.isCurrent(request)) setConflict(current => current?.base === snapshot.base
        ? { ...current, error: '读取未完成 · 重试' } : current);
    } finally { if (gate.isCurrent(request)) setReading(false); }
  }
  async function save(event) {
    event.preventDefault();
    if (disabled || saving || reading || conflict || !['pending', 'active'].includes(base.state)) return;
    const request = gate.issue('save', scope);
    if (!request) return;
    setSaving(true); setError('');
    try {
      const result = await libraryApi.edit(projectId, base, text, conditions.split('\n').map(value => value.trim()).filter(Boolean));
      if (gate.isCurrent(request)) onSaved(result);
    } catch (failure) {
      if (!gate.isCurrent(request)) return;
      if (failure.status === 409) {
        // 冲突保留本次原版与编辑内容，读回之后仍由用户选择版本。
        const snapshot = { base, server: null, data: null, error: '' };
        setConflict(snapshot);
        await readConflict(snapshot);
      } else setError(failure.message);
    } finally { if (gate.isCurrent(request)) setSaving(false); }
  }
  function choose(server) {
    const next = conflict.server;
    if (server) { setText(next.text); setConditions((next.conditions || []).join('\n')); }
    setBase(next); setConflict(null); setError(''); onAdopt(conflict.data);
    if (conflict.data.readonly || !['pending', 'active'].includes(next.state)) onCancel();
  }
  return <form className="library-edit" onSubmit={save}>
    {error && <div className="library-error" role="alert"><span>{error}</span></div>}
    {conflict?.server && <DraftConflictView compact baseDraft={insightDraft(conflict.base)} editor={{ text, conditions }}
      server={insightDraft(conflict.server)} fields={[["text", "认识"], ["conditions", "条件"]]}
      confirmed={conflict.data.readonly || !['pending', 'active'].includes(conflict.server.state)}
      onChoose={choose} disabled={disabled || saving || reading}/>}
    {conflict && !conflict.server && <div className="library-error" role={reading ? 'status' : 'alert'}>
      <span>{reading ? '读取中' : conflict.error}</span>{conflict.error && <button type="button" disabled={disabled || reading}
        onClick={() => readConflict(conflict)}>重试</button>}
    </div>}
    <label>认识<textarea aria-label="认识正文" disabled={disabled || saving} value={text} onChange={event => setText(event.target.value)}/></label>
    <label>条件<textarea aria-label="适用条件" disabled={disabled || saving} value={conditions} onChange={event => setConditions(event.target.value)}/></label>
    <button type="submit" disabled={disabled || saving || reading || Boolean(conflict) || !['pending', 'active'].includes(base.state)}>保存</button>
    <button type="button" onClick={onCancel}>取消</button>
  </form>;
}

export function Library({ projectId = 'default', projects = [], initialLayer = 'insight', initialFilter = null, documentId, sourceId, onNavigate, onProjectCreated }) {
  const [layer, setLayer] = useState(layers.includes(initialLayer) ? initialLayer : 'insight'), [scene, setScene] = useState(''), [q, setQ] = useState('');
  const [filter, setFilter] = useState(initialFilter), [loaded, setLoaded] = useState(null), [refresh, setRefresh] = useState(0);
  const [inboxCount, setInboxCount] = useState(null);
  const [inbox, setInbox] = useState(false), [suggestions, setSuggestions] = useState(null);
  const [projectSuggestions, setProjectSuggestions] = useState(null);
  const readProject = inbox ? 'inbox' : projectId;
  const [selection, setSelection] = useState(null), [error, setError] = useState(''), [mutating, setBusy] = useState(false);
  const [failedDrill, setFailedDrill] = useState(null), [userEpoch, setUserEpoch] = useState(0);
  const [placementNoticeScope, setPlacementNoticeScope] = useState(null);
  const [group, setGroup] = useState(null), [libraryMore, setLibraryMore] = useState(false), [constraintCount, setConstraintCount] = useState(null), [more, setMore] = useState(false), [maintenance, setMaintenance] = useState(null);
  const insightMoreTrigger = useRef(null);
  const [editing, setEditing] = useState(false), [editBusy, setEditBusy] = useState(false);
  const busy = mutating || editBusy;
  const [skillExport, setSkillExport] = useState(null);
  const [consolidation, setConsolidation] = useState(null), [consolidating, setConsolidating] = useState(false);
  const [consolidationRead, setConsolidationRead] = useState(0);
  const [reviewing, setReviewing] = useState(false);
  const reviews = useSignalReviews(projectId, refresh);
  const gaps = useGaps(projectId, refresh);
  const consolidationGate = useRequestScope(projectId), consolidationScope = consolidationGate.scope, consolidationPending = useRef(null);
  const scope = `${projectId}\0${inbox}\0${scene}\0${q}`;
  const drillGate = useRequestScope(`${scope}\0${filter}\0${userEpoch}`), drillScope = drillGate.scope;
  const drillError = drillGate.isCurrent(failedDrill?.request) ? failedDrill : null;
  const placementNotice = drillGate.isScopeCurrent(placementNoticeScope);
  const currentScope = useRef(scope), drillGeneration = useRef(0);
  currentScope.current = scope;
  useEffect(() => {
    const changed = () => {
      drillGate.invalidate('detail'); drillGeneration.current += 1;
      setFailedDrill(null); setSelection(null); setError(''); setUserEpoch(value => value + 1);
    };
    window.addEventListener('chriptmas-user-space', changed);
    window.addEventListener('chriptmas-device-required', changed);
    return () => {
      window.removeEventListener('chriptmas-user-space', changed);
      window.removeEventListener('chriptmas-device-required', changed);
    };
  }, []);
  const visible = loaded?.scope === scope ? loaded.data : null;
  const chosen = selection?.scope === scope ? selection : null;
  useEffect(() => {
    const insight = chosen?.data?.insight;
    if (chosen?.layer === 'insight' && insight?.state === 'pending')
      sendSignal({ kind: 'view', project_id: chosen.sourceProject, object: { kind: 'insight', id: insight.id, revision: insight.revision } }, chosen.signalToken);
  }, [chosen]);
  const detailProject = chosen?.sourceProject || readProject;
  const project = projects.find(row => row.id === projectId);
  useEffect(() => { setReviewing(false); setInbox(false); setSuggestions(null); setScene(''); setQ(''); setFilter(initialFilter); setSelection(null); setGroup(null); setLibraryMore(false); setMore(false); setMaintenance(null); setEditing(false); setBusy(false); setError(''); setConsolidation(null); setConsolidating(false); consolidationPending.current = null; }, [projectId, initialFilter]);
  useEffect(() => {
    if (documentId) { setLayer('note'); open('note', { document_id: documentId }); }
    else if (sourceId) { setLayer('source'); open('source', { id: sourceId }); }
    return () => { drillGeneration.current += 1; };
  }, [projectId, documentId, sourceId]);
  useEffect(() => {
    const controller = new AbortController();
    const request = consolidationGate.issue('status', consolidationScope);
    let timer, failures = 0, running = consolidation?.project === projectId && consolidation.running;
    const current = () => !controller.signal.aborted && consolidationGate.isCurrent(request);
    async function readStatus() {
      if (!current()) return;
      try {
        const value = await libraryApi.consolidation(projectId, { signal: controller.signal });
        if (!current()) return;
        setConsolidation({ project: projectId, ...value });
        if (running && !value.running) setRefresh(value => value + 1);
        running = value.running; failures = 0;
      } catch {
        if (!current() || !running || ++failures >= 3) return;
      }
      if (running) timer = setTimeout(readStatus, 1500);
    }
    readStatus();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [projectId, refresh, consolidationRead]);
  async function consolidateNow() {
    if (consolidationPending.current === consolidationScope) return;
    const request = consolidationGate.issue('consolidate', consolidationScope);
    if (!request) return;
    consolidationPending.current = consolidationScope;
    setConsolidating(true);
    try {
      const result = await libraryApi.consolidate(projectId);
      if (consolidationGate.isCurrent(request)) {
        consolidationGate.invalidate('status');
        // 记住已接受的任务，首次回读即已完成时也刷新列表。
        setConsolidation(value => ({ ...value, project: projectId, running: true, job_id: result.job_id }));
        setLibraryMore(false); setRefresh(value => value + 1);
      }
    } catch (reason) {
      if (consolidationGate.isCurrent(request)) {
        if (reason.code === 'consolidate_limit') {
          consolidationGate.invalidate('status');
          setConsolidation(value => ({ ...value, project: projectId, limit: true }));
        }
        else setError(reason.message);
      }
    } finally {
      if (consolidationGate.isCurrent(request)) { consolidationPending.current = null; setConsolidating(false); }
    }
  }
  useEffect(() => {
    const controller = new AbortController();
    setError('');
    Promise.all([...layers.map(key => libraryApi.list(key, readProject, { scene, q, signal: controller.signal })), loadArchivedDocuments({ projectId: readProject })])
      .then(values => { if (!controller.signal.aborted && currentScope.current === scope) setLoaded({ scope, data: { ...Object.fromEntries(layers.map((key, i) => [key, values[i]])), archived: values[4].items || [] } }); })
      .catch(reason => { if (!controller.signal.aborted && currentScope.current === scope) setError(reason.message); });
    return () => controller.abort();
  }, [scope, refresh]);
  useEffect(() => {
    const controller = new AbortController();
    libraryApi.list('insight', 'inbox', { signal: controller.signal }).then(result => {
      if (!controller.signal.aborted) setInboxCount((result.counts?.pending || 0) + (result.counts?.active || 0));
    }).catch(() => { if (!controller.signal.aborted) setInboxCount(null); });
    return () => controller.abort();
  }, [projectId, refresh]);
  useEffect(() => {
    let current = true;
    setConstraintCount(null);
    recognitionApi.loadConstraints({ projectId }).then(result => {
      if (current) setConstraintCount((result.items || []).filter(row => row.enabled).length);
    }).catch(() => {});
    return () => { current = false; };
  }, [projectId]);
  useEffect(() => {
    setSuggestions(null);
    if (!inbox || projectId === 'inbox') return;
    const controller = new AbortController();
    libraryApi.inboxSuggestions(projectId, { signal: controller.signal }).then(result => {
      if (!controller.signal.aborted) setSuggestions({ project: projectId, values: Object.fromEntries((result.items || []).map(row => [row.id, row.scene])) });
    }).catch(reason => { if (!controller.signal.aborted) setError(reason.message); });
    return () => controller.abort();
  }, [projectId, inbox, refresh]);
  useEffect(() => {
    setProjectSuggestions(null);
    if (readProject !== 'inbox') return;
    const controller = new AbortController();
    Promise.all([libraryApi.inboxProjects({ signal: controller.signal }),
      libraryApi.list('insight', 'inbox', { signal: controller.signal })]).then(([result, originals]) => {
      if (!controller.signal.aborted && currentScope.current === scope)
        setProjectSuggestions({ scope, items: result.items || [], rows: originals.items || [] });
    }).catch(reason => { if (!controller.signal.aborted && currentScope.current === scope) setError(reason.message); });
    return () => controller.abort();
  }, [scope, refresh]);
  async function documentArchived(targetProject, data, readonly) {
    if (!data.note || data.readonly || readonly) return false;
    const result = await loadArchivedDocuments({ projectId: targetProject });
    if (!Array.isArray(result.items)) throw new Error('读取未完成 · 重试');
    return result.items.some(row => row.document_id === data.note.document_id);
  }
  async function open(entryLayer, row, targetProject = readProject, readonly = false) {
    const request = drillGate.issue('detail', drillScope);
    if (!request) return;
    const generation = ++drillGeneration.current, signalToken = signalEpoch();
    const identity = row.id || row.document_id;
    setSelection(null); setGroup(null); setLibraryMore(false); setMore(false); setMaintenance(null); setEditing(false); setError(''); setFailedDrill(null);
    try {
      const data = await libraryApi.drill(targetProject, entryLayer, identity);
      const archived = await documentArchived(targetProject, data, readonly);
      if (drillGate.isCurrent(request) && generation === drillGeneration.current) {
        setSelection({ scope, sourceProject:targetProject, layer: entryLayer, entered: entryLayer, row, data, archived, readonly, signalToken });
        libraryApi.opened(targetProject, entryLayer, identity);
      }
    } catch (reason) {
      if (drillGate.isCurrent(request) && generation === drillGeneration.current)
        setFailedDrill({ request, message: reason.message, retry: () => open(entryLayer, row, targetProject, readonly) });
    }
  }
  async function selectDrill(options) {
    const selected = chosen;
    if (!selected) return;
    const request = drillGate.issue('detail', drillScope);
    if (!request) return;
    const generation = ++drillGeneration.current;
    setError(''); setFailedDrill(null);
    try {
      const data = await libraryApi.drill(selected.sourceProject || readProject, selected.entered, selected.row.id || selected.row.document_id, options);
      const archived = await documentArchived(selected.sourceProject || readProject, data, selected.readonly);
      if (drillGate.isCurrent(request) && generation === drillGeneration.current) {
        setSelection(value => value && value.scope === selected.scope
          && value.sourceProject === selected.sourceProject && value.entered === selected.entered
          && value.row === selected.row && value.data === selected.data ? { ...value, data, archived } : value);
        if (options.document_id) libraryApi.opened(selected.sourceProject || readProject, 'document', options.document_id);
        setEditing(false); setMore(false); setMaintenance(null);
      }
    } catch (reason) {
      if (drillGate.isCurrent(request) && generation === drillGeneration.current)
        setFailedDrill({ request, message: reason.message, retry: () => selectDrill(options) });
    }
  }
  async function reloadMaintenance() {
    const selected=chosen;
    if (!selected?.data.insight) return null;
    const request=drillGate.issue('detail',drillScope);
    if (!request) return null;
    const generation=++drillGeneration.current;
    const targetProject=selected.sourceProject || readProject, identity=selected.data.insight.id;
    const itemsRequest=libraryApi.list('insight',targetProject);
    const [items,visibleItems,data]=await Promise.all([
      itemsRequest,
      scene || q ? libraryApi.list('insight',targetProject,{scene,q}) : itemsRequest,
      libraryApi.drill(targetProject,'insight',identity).catch(async failure=>{
        if (failure.status!==404) throw failure;
        const history=await recognitionApi.loadRecognitionVersions({projectId:targetProject,recognitionId:identity});
        const current=history.versions[0], snapshot=current?.snapshot;
        if (!snapshot || !['superseded','revoked'].includes(snapshot.state)) throw failure;
        return {...selected.data,readonly:true,insight:{...selected.data.insight,text:snapshot.content,
          revision:current.recognition_revision,state:snapshot.state,conditions:snapshot.conditions,
          source_count:new Set([...snapshot.source_experience_ids,...snapshot.source_recognition_ids]).size}};
      }),
    ]);
    const archived = await documentArchived(targetProject, data, selected.readonly);
    if (!drillGate.isCurrent(request) || generation!==drillGeneration.current) return null;
    setSelection(value=>value===selected ? {...value,data,archived,readonly:value.readonly || Boolean(data.readonly)} : value);
    setRefresh(value=>value+1);
    return {items:items.items,visibleItems:visibleItems.items};
  }
  async function mutate(operation) {
    if (busy) return;
    drillGate.invalidate('detail'); setFailedDrill(null);
    setBusy(true); setError('');
    try {
      const result = await operation();
      if (currentScope.current !== scope) return;
      setSelection(value => {
        if (!value || value.scope !== scope || value.data !== chosen?.data) return value;
        if (result.lifecycle) return null;
        const data = { ...value.data };
        if (result.document_id && data.note?.document_id === result.document_id) data.note = { ...data.note, verified: true };
        else if (result.text) data.insight = result;
        else data.insight = null;
        return { ...value, data };
      });
      setEditing(false); setRefresh(value => value + 1);
    } catch (reason) { if (currentScope.current === scope) setError(reason.message); }
    finally { if (currentScope.current === scope) setBusy(false); }
  }
  const counts = Object.fromEntries(layers.map(key => [key, visible ? visible[key]?.items?.length || 0 : null]));
  const archivedIds = new Set((visible?.archived || []).map(row => row.document_id));
  const noteState = row => archivedIds.has(row.document_id) ? 'archived' : row.verified ? 'verified' : 'unverified';
  const noteCounts = Object.fromEntries(['unverified', 'verified', 'archived'].map(key => [key, (visible?.note?.items || []).filter(row => noteState(row) === key).length]));
  const rows = (visible?.[layer]?.items || []).filter(row => filter === null || (layer === 'insight' ? row.state === filter : layer !== 'note' || noteState(row) === filter));
  const noteArchived = chosen?.data.note && chosen.archived;
  const noteCooled = chosen?.data.note?.recall_state === 'cooled';
  const insight = chosen?.data.insight;
  const insightRecoverable = insight?.state === 'forgotten' || insight?.recall_state === 'cooled';
  const sourceProject = chosen?.data.source_project_id || detailProject;
  const readonlySource = chosen?.readonly || chosen?.data.readonly;
  const destination = projects.find(row => row.id === insight?.hint?.scope_hint && row.id !== detailProject);
  const original = chosen?.data.source || (chosen?.entered === 'source' ? chosen.row : null);
  const focusId = { insight: insight?.id, summary: chosen?.data.summary?.document_id,
    note: chosen?.data.note?.document_id, source: original?.id }[chosen?.layer] || chosen?.row?.id || chosen?.row?.document_id;
  const focusScope = chosen ? JSON.stringify([chosen.layer === 'source' ? sourceProject : detailProject, chosen.layer, focusId]) : undefined;
  function jump(next) {
    drillGate.invalidate('detail'); setFailedDrill(null);
    setEditing(false); setMore(false); setMaintenance(null);
    setSelection(value => value && { ...value, layer: next });
    const identity = next === 'insight' ? chosen?.data.insight?.id : chosen?.data.note?.document_id;
    if (identity && next !== chosen?.layer) libraryApi.opened(detailProject, next, identity);
  }
  const panelActions = !readonlySource && chosen?.layer === 'note' && chosen.data.note && !chosen.data.note.verified && !noteArchived
    ? <button type="button" disabled={busy} onClick={() => mutate(() => libraryApi.verify(detailProject, chosen.data.note))}>核对完成</button> : null;
  function clearReviewDrill() {
    drillGate.invalidate('detail'); drillGeneration.current += 1; setFailedDrill(null); setSelection(null); setError('');
  }
  const retryDrill = drillError && <div className="library-error" role="alert"><span>{drillError.message}</span><button type="button" onClick={() => { if (drillGate.isCurrent(drillError.request)) drillError.retry(); }}>重试</button></div>;
  return <div className={`library-page ${chosen || group || reviewing ? 'has-focus' : ''}`}>
    <aside className="library-scenes" aria-label="场景">
      <button type="button" aria-pressed={!inbox && !scene} onClick={() => { setInbox(false); setScene(''); setSelection(null); }}>全部</button>
      {(project?.scenes || []).map(name => <button type="button" key={name} aria-pressed={!inbox && scene === name} onClick={() => { setInbox(false); setScene(name); setSelection(null); }}>{name}</button>)}
      <div className="library-scene-divider"/>
      <button type="button" aria-pressed={inbox || projectId === 'inbox'} onClick={() => { setInbox(true); setScene(''); setFilter(null); setLayer('insight'); setSelection(null); setGroup(null); }}>收件箱 <span className="library-meta">{inboxCount ?? '—'}</span></button>
      <button type="button" aria-pressed={projectId === 'me'} onClick={() => onNavigate?.('library', { project_id: 'me' })}>我</button>
    </aside>
    {reviewing ? <SignalReviews key={projectId} projectId={projectId} reviews={reviews} onBack={() => { clearReviewDrill(); setReviewing(false); }}
      onOpen={object => open(object.kind === 'document' ? 'note' : 'insight', { id: object.id }, projectId)} onSelect={clearReviewDrill} drillError={drillError?.message || error} hideEvidence={Boolean(chosen)} onChanged={() => setRefresh(value => value + 1)}/>
    : <main className="library-main">
      <LayerTabs value={layer} counts={counts} onChange={next => { clearReviewDrill(); setInbox(false); setLayer(next); setFilter(null); }}>
        <div className="library-search-tools"><label className="library-search"><Icon name="search"/><input type="search" aria-label="搜索" value={q} onChange={event => { setQ(event.target.value); setSelection(null); }}/></label>
          <div className="library-more" onKeyDown={event => { if (event.key === 'Escape') { setLibraryMore(false); event.currentTarget.querySelector('button')?.focus(); } }}>
            <button type="button" className="library-more-trigger" aria-label="资料库更多" aria-haspopup="menu" aria-expanded={libraryMore} onClick={() => { setLibraryMore(value => !value); if (!libraryMore) { setConsolidationRead(value => value + 1); reviews.reload(); gaps.reload(); } }}>⋯</button>
            {libraryMore && <div className="library-more-menu" role="menu" aria-label="资料库更多">
              <button type="button" role="menuitem" title="满10次自动整理" disabled={consolidating || consolidation?.project !== projectId || consolidation?.limit}
                onClick={consolidateNow}>现在整理 <span>{consolidation?.project === projectId ? consolidation.score : '—'}/10</span></button>
              <button type="button" role="menuitem" onClick={() => { setLibraryMore(false); onNavigate?.('settings', { project_id: projectId, section: 'project', expand_project: projectId }); }}>约束 <span>{constraintCount ?? '—'}</span><Icon name="open" size={14}/></button>
              <button type="button" role="menuitem" onClick={() => { setLibraryMore(false); setSelection(null); setGroup('memo'); }}>备忘</button>
              {reviews.items.length > 0 && <button type="button" role="menuitem" onClick={() => { clearReviewDrill(); setLibraryMore(false); setGroup(null); setReviewing(true); }}>纠偏 <span>{reviews.items.length}</span></button>}
              {gaps.items.length > 0 && <button type="button" role="menuitem" onClick={() => { setLibraryMore(false); setSelection(null); setReviewing(false); setGroup('gaps'); gaps.reload(); }}>待补 <span>{gaps.items.length}</span></button>}
              {gaps.error && <div className="library-error" role="alert"><span>{gaps.error}</span><button type="button" aria-label="重试待补" onClick={gaps.reload}>重试</button></div>}
              {reviews.error && <div className="library-error" role="alert"><span>{reviews.error}</span><button type="button" aria-label="重试纠偏" onClick={reviews.reload}>重试</button></div>}
            </div>}
          </div>
        </div>
      </LayerTabs>
      {layer === 'note' && <FilterBar label="整理稿状态" filters={[{ key: 'unverified', label: '未核对', dot: 'unverified' }, { key: 'verified', label: '已核对', dot: 'done' }, { key: 'archived', label: '已遗忘', dot: 'forgotten-user' }]} value={filter} counts={noteCounts} onChange={value => setFilter(old => old === value ? null : value)}/> }
      {layer === 'insight' && <FilterBar value={filter} counts={visible?.insight?.counts || {}} onChange={value => setFilter(old => old === value ? null : value)}/>}
      {error && <div className="library-error" role="alert"><span>{error}</span><button type="button" onClick={() => setRefresh(value => value + 1)}>重试</button></div>}
      {placementNotice && <div className="library-error" role="status">已移动 · 认识提炼未完成</div>}
      {!chosen && retryDrill}
      {readProject === 'inbox' && layer === 'insight' && projectSuggestions?.scope === scope && projectSuggestions.items.map(group =>
        <InboxProjectSuggestion key={`${scope}:${group.ids.join(':')}`} group={group} rows={projectSuggestions.rows} scope={scope}
          onProjectCreated={onProjectCreated} onChanged={() => { setSelection(null); setRefresh(value => value + 1); }}/>) }
      <div className="library-rows" aria-label={labels[layer]}>
        {rows.map(row => <Row key={row.id || row.document_id} dot={layer === 'insight' ? insightDot(row) : layer === 'note' && (noteState(row) === 'archived' || row.recall_state === 'forgotten') ? 'forgotten-user' : row.recall_state === 'cooled' ? 'cooled' : layer === 'source' || row.verified ? 'done' : 'unverified'}
          title={row.pattern || (layer === 'insight' && row.state === 'pending' && row.hint) ? <>{row.pattern && <Icon name="pattern" size={14}/>} {row.text || row.title}{row.state === 'pending' && <CandidateHint relation={row.hint?.relation} commentSource={row.comment_source}/>}</> : row.text || row.title} sub={layer === 'summary' ? row.summary : undefined}
          meta={layer === 'insight' ? [row.scene, `${row.source_count} 源`].filter(Boolean).join(' · ') : [row.filing ? '已移到 #' + (projects.find(project=>project.id===row.filing.target_project_id)?.name || row.filing.target_project_id) : layer === 'source' ? kinds[row.kind] || '原件' : layer === 'note' && noteState(row) === 'archived' ? '已遗忘' : row.verified ? '已核对' : '未核对', date(row.created_at)].filter(Boolean).join(' · ')}
          selected={chosen && (Boolean(row.id) && chosen.data.insight?.id === row.id || Boolean(row.document_id) && chosen.data.note?.document_id === row.document_id)} onOpen={() => open(layer, row)} trailing={inbox && projectId !== 'inbox' && layer === 'insight' && ['pending', 'active'].includes(row.state) ? <InboxFiling key={`${scope}:${row.id}:${row.revision}`} row={row} targetProjectId={projectId} scenes={project?.scenes || []} suggestion={suggestions?.project === projectId ? suggestions.values[row.id] : null} onFiled={() => { setSelection(null); setError(''); setRefresh(value => value + 1); }} onError={setError}/> : undefined}/>)}
      </div>
    </main>}
    {group === 'gaps' && <Gaps key={projectId} projectId={projectId} gaps={gaps} onClose={() => setGroup(null)} onOpen={row => onNavigate?.('workbench', { project_id: projectId, compose: { scene: row.scene, intent: 'remember' } })}/>}
    {group === 'memo' && <MemoPanel key={projectId} projectId={projectId} onClose={()=>setGroup(null)}/>}
    {chosen && !(skillExport?.scope === scope && skillExport.sourceId === chosen.data.insight?.id) && <FocusPanel className="library-focus" title={labels[chosen.layer]} focusScope={focusScope} status={chosen.layer === 'note' && chosen.data.note ? noteArchived ? 'forgotten-user' : chosen.data.note.verified ? 'done' : 'unverified' : undefined} actions={panelActions} onClose={() => { clearReviewDrill(); setEditing(false); }}>
      <Breadcrumb current={chosen.layer} onJump={jump}/>
      {retryDrill}
      {chosen.data.documents?.length > 1 && <section aria-label="整理稿选择">{chosen.data.documents.map(row => <Row key={row.document_id} title={row.title} selected={chosen.data.note?.document_id === row.document_id} onOpen={() => selectDrill({ document_id: row.document_id })}/>)}</section>}
      {chosen.layer === 'source' && chosen.entered !== 'source' && chosen.data.sources?.length > 1 && <section aria-label="原件选择">{chosen.data.sources.map(row => <Row key={row.id} title={row.title} selected={chosen.data.source?.id === row.id} onOpen={() => selectDrill({ document_id: chosen.data.note?.document_id, source_id: row.id })}/>)}</section>}
      {chosen.layer === 'insight' && <>
        {chosen.entered !== 'insight' && <section className="library-grown"><h3>长出的认识</h3>{chosen.data.grown.map(row => <button type="button" key={row.id} onClick={() => open('insight', row)}>{row.text}</button>)}</section>}
        {insight && <>
          {editing ? <InsightEditor key={`${scope}:${filter}:${userEpoch}:${detailProject}:${chosen.entered}:${chosen.row.id || chosen.row.document_id}`}
            projectId={detailProject} insight={insight} disabled={mutating} onBusy={setEditBusy} onCancel={() => setEditing(false)}
            onAdopt={data => { drillGate.invalidate('detail'); setFailedDrill(null);
              setSelection(value => value === chosen ? { ...value, data, readonly: value.readonly || Boolean(data.readonly) } : value);
              setRefresh(value => value + 1); }}
            onSaved={result => { drillGate.invalidate('detail'); setFailedDrill(null);
              setSelection(value => value === chosen ? { ...value, data: { ...value.data, insight: result } } : value);
              setEditing(false); setRefresh(value => value + 1); }}/>
            : <p className="library-insight-text">{insight.text}</p>}
          <p className="library-meta">{[insight.scene, `${insight.source_count} 源`].filter(Boolean).join(' · ')}</p>
          {insight.conditions?.length > 0 && <ul className="library-conditions">{insight.conditions.map(value => <li key={value}>{value}</li>)}</ul>}
          {insight.hint?.target && <section className="library-grown" aria-label="旧认识">
            <Row readOnly title={insight.hint.target.text}/>
            {insight.hint.target.conditions?.length > 0 && <ul className="library-conditions">{insight.hint.target.conditions.map(value => <li key={value}>{value}</li>)}</ul>}
            <button type="button" className="library-comparison-open" aria-label="打开旧认识" onClick={() => open('insight', insight.hint.target, insight.hint.target.project_id, true)}><Icon name="open" size={14}/></button>
          </section>}
          {!chosen.readonly && <div className="library-actions">
            {insight.state === 'pending' && <><button type="button" className="library-primary" disabled={busy} onClick={() => mutate(() => libraryApi.review(detailProject, insight, 'confirm'))}>确认</button><button type="button" disabled={busy} onClick={() => mutate(() => libraryApi.review(detailProject, insight, 'drop'))}>丢弃</button></>}
            {insight.state === 'pending' && destination && <button type="button" disabled={busy} aria-label={destination.id === 'me' ? '确认到我' : `确认到 #${destination.name}`} onClick={() => mutate(async () => { await libraryApi.confirmTo(detailProject, insight, destination.id); return { lifecycle: true }; })}>{destination.id === 'me' ? <Icon name="person" size={16}/> : `#${destination.name}`}</button>}
            {['pending', 'active'].includes(insight.state) && <button type="button" disabled={busy} onClick={() => { drillGate.invalidate('detail'); setFailedDrill(null); setEditing(true); }}>编辑</button>}
            {insight.kind === 'candidate' && insight.state === 'forgotten' && <button type="button" disabled={busy} onClick={() => mutate(() => libraryApi.forget(detailProject, insight, false))}>捡回</button>}
            {insight.kind === 'recognition' && ['active', 'forgotten'].includes(insight.state) && <button type="button" disabled={busy} onClick={() => mutate(() => libraryApi.forget(detailProject, insight, !insightRecoverable))}>{insightRecoverable ? '恢复' : '遗忘'}</button>}
            {insight.kind === 'recognition' && <button ref={insightMoreTrigger} type="button" aria-expanded={more} onClick={()=>setMore(value=>!value)}>更多</button>}
          </div>}
          {more && <div className="library-actions">{[['split','拆分'],['merge','合并'],['versions','版本'],['erase','永久删除']].filter(([key])=>!['split','merge'].includes(key)||insight.state==='active').map(([key,name])=><button key={key} type="button" onClick={()=>{drillGate.invalidate('detail');setMaintenance(key);setMore(false);if(key==='versions')insightMoreTrigger.current?.focus();}}>{name}</button>)}
            {!readonlySource && insight.kind === 'recognition' && insight.state === 'active' && insight.conditions?.length > 0 && <button type="button" onClick={() => { setSkillExport({ scope, projectId: detailProject, scene: insight.scene, sourceId: insight.id }); setMore(false); }}>导出为 skill</button>}
          </div>}
          {maintenance && <InsightMaintenance key={`${scope}:${filter}:${userEpoch}:${detailProject}:${insight.id}:${maintenance}`} projectId={detailProject} insight={insight} insights={visible?.insight?.items || []} action={maintenance} onReload={reloadMaintenance} onDone={()=>{setSelection(null);setMore(false);setMaintenance(null);setRefresh(value=>value+1);}}/>}
          {insight.kind === 'recognition' && <InsightLinks key={`${detailProject}:${insight.id}`} projectId={detailProject} insightId={insight.id} insights={visible?.insight?.items || []} readonly={chosen.readonly} onOpen={row => open('insight', row, detailProject, chosen.readonly)} onReviewed={() => setRefresh(value => value + 1)}/>}
          <ConsolidationSuggestions key={`consolidation:${detailProject}:${insight.id}:${insight.revision}`} projectId={detailProject} insightId={insight.id} originals={insight.merged_from} readonly={chosen.readonly} onOpenDocument={(id,sourceProject)=>open('note',{document_id:id},sourceProject, chosen.readonly)} onReviewed={()=>{setSelection(null);setRefresh(value=>value+1);}}/>
          {insight.related?.length > 0 && <section className="library-grown"><h3>相关</h3>{insight.related.map(row => <button type="button" key={row.id} onClick={() => open('insight', row, detailProject, chosen.readonly)}>{row.text}</button>)}</section>}
        </>}
      </>}
      {chosen.layer === 'summary' && chosen.data.summary && <><p className="library-meta">{date(chosen.data.summary.created_at)}</p><h3 className="library-title">{chosen.data.summary.title}</h3><p className="library-summary-text">{chosen.data.summary.text}</p></>}
      {chosen.layer === 'note' && chosen.data.note && <>{readonlySource || noteArchived
        ? <MarkdownBody omitEmptyArtifacts evidence={chosen.data.note.facts.filter(fact => fact.evidence.quote === chosen.data.source?.window?.quote)} onEvidence={chosen.data.source?.window ? () => jump('source') : undefined}
          documentId={chosen.data.note.document_id} documentRevision={chosen.data.note.revision} commentSection={chosen.data.note.comment_section}>{chosen.data.note.markdown}</MarkdownBody>
        : <DocumentMarkdownEditor key={`${scope}:${detailProject}:${chosen.data.note.document_id}`} projectId={detailProject} documentId={chosen.data.note.document_id} document={chosen.data.note} disabled={busy}
          evidence={chosen.data.note.facts.filter(fact => fact.evidence.quote === chosen.data.source?.window?.quote)} onEvidence={chosen.data.source?.window ? () => jump('source') : undefined}
          readDocument={async () => (await libraryApi.drill(detailProject, 'note', chosen.data.note.document_id)).note}
          onSaved={document => { drillGate.invalidate('detail'); setFailedDrill(null); setSelection(value => value?.scope === scope && value.data === chosen.data ? { ...value, data: { ...value.data, note: { ...value.data.note, ...document, verified: true } } } : value); setRefresh(value => value + 1); }}/>
      }
        <DocumentPlacement key={`${detailProject}:${chosen.data.note.document_id}:${chosen.data.note.revision}`} projectId={detailProject}
          projects={projects} row={chosen.data.note} readonly={Boolean(readonlySource || noteArchived)} onError={setError}
          onChanged={result => {
            if (!drillGate.isScopeCurrent(drillScope)) return;
            setPlacementNoticeScope(result.state === 'filed' && result.error ? drillScope : null);
            setSelection(null); setRefresh(value => value + 1);
          }}/>
        {!readonlySource && <div className="library-actions"><button type="button" disabled={busy} onClick={()=>mutate(async()=>{
          if (noteCooled && !noteArchived) await libraryApi.restoreRecall(detailProject, chosen.data.note);
          else await (noteArchived ? restoreDocument : archiveDocument)({projectId:detailProject,documentId:chosen.data.note.document_id,expectedRevision:chosen.data.note.revision});
          return {lifecycle:true};
        })}>{noteArchived || noteCooled ? '恢复' : '遗忘'}</button></div>}
      </>}
      {chosen.layer === 'source' && original && <><p className="library-meta">{kinds[original.kind] || '原件'}</p><h3 className="library-title">{original.title}</h3>
        {!readonlySource && !chosen.data.source_readonly && <OriginalPrivacy key={`${sourceProject}:${original.id}`} projectId={sourceProject} sourceId={original.id}/>}
        <OriginalText key={`${scope}:${original.id}:${chosen.data.note?.document_id || ''}`} projectId={sourceProject} original={original}/>
        <OriginalFiles key={`${scope}:${filter}:${userEpoch}:${sourceProject}:${original.id}`} projectId={sourceProject} original={original} scopeKey={`${scope}:${filter}:${userEpoch}:${sourceProject}:${original.id}`}/>
      </>}
      {layers.indexOf(chosen.layer) < 3 && (chosen.data[layers[layers.indexOf(chosen.layer) + 1]] || (chosen.layer === 'insight' && chosen.data.documents?.length > 1) || (chosen.layer === 'note' && chosen.data.sources?.length > 1)) && <button type="button" className="library-down" aria-label="下一层" onClick={() => jump(layers[layers.indexOf(chosen.layer) + 1])}>↓</button>}
    </FocusPanel>}
    {chosen && skillExport?.scope === scope && skillExport.sourceId === chosen.data.insight?.id && <SkillExportPanel key={`${scope}:${detailProject}:${skillExport.sourceId}`} projectId={skillExport.projectId} scene={skillExport.scene} sourceId={skillExport.sourceId} onClose={() => setSkillExport(null)}/>}
  </div>;
}
export default Library;
