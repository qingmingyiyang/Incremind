import { ConsolidationSuggestions } from './ConsolidationSuggestions';
import { InsightLinks } from './InsightLinks';
import { useEffect, useRef, useState } from 'react';
import { LayerTabs, FilterBar, Row, Breadcrumb, FocusPanel, Icon, MarkdownBody, CandidateHint } from '../../shared/ui';
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
import './Library.css';

const layers = ['insight', 'summary', 'note', 'source'];
const labels = { insight: '认识', summary: '摘要', note: '整理稿', source: '原件' };
const dots = { pending: 'pending', active: 'done', stale: 'unverified', forgotten: 'forgotten' };
const insightDot = row => row.state === 'forgotten' ? row.recall_by === 'auto' ? 'forgotten' : 'forgotten-user'
  : row.recall_state === 'cooled' ? 'cooled' : dots[row.state];
const kinds = { text: '文字', link: '链接', file: '文件', audio: '录音', image: '图片', video: '视频', other: '原件' };
import { OriginalPrivacy } from './OriginalPrivacy';

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

const date = value => value ? String(value).slice(0, 10).replaceAll('-', '·') : '';

export function Library({ projectId = 'default', projects = [], initialLayer = 'insight', documentId, onNavigate, onProjectCreated }) {
  const [layer, setLayer] = useState(layers.includes(initialLayer) ? initialLayer : 'insight'), [scene, setScene] = useState(''), [q, setQ] = useState('');
  const [filter, setFilter] = useState(null), [loaded, setLoaded] = useState(null), [refresh, setRefresh] = useState(0);
  const [inboxCount, setInboxCount] = useState(null);
  const [inbox, setInbox] = useState(false), [suggestions, setSuggestions] = useState(null);
  const [projectSuggestions, setProjectSuggestions] = useState(null);
  const readProject = inbox ? 'inbox' : projectId;
  const [selection, setSelection] = useState(null), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const [group, setGroup] = useState(null), [libraryMore, setLibraryMore] = useState(false), [constraintCount, setConstraintCount] = useState(null), [more, setMore] = useState(false), [maintenance, setMaintenance] = useState(null);
  const [editing, setEditing] = useState(false), [text, setText] = useState(''), [conditions, setConditions] = useState('');
  const [skillExport, setSkillExport] = useState(null);
  const [consolidation, setConsolidation] = useState(null), [consolidating, setConsolidating] = useState(false);
  const [consolidationRead, setConsolidationRead] = useState(0);
  const [reviewing, setReviewing] = useState(false);
  const reviews = useSignalReviews(projectId, refresh);
  const gaps = useGaps(projectId, refresh);
  const consolidationGate = useRequestScope(projectId), consolidationScope = consolidationGate.scope, consolidationPending = useRef(null);
  const scope = `${projectId}\0${inbox}\0${scene}\0${q}`;
  const currentScope = useRef(scope), drillGeneration = useRef(0);
  currentScope.current = scope;
  const visible = loaded?.scope === scope ? loaded.data : null;
  const chosen = selection?.scope === scope ? selection : null;
  useEffect(() => {
    const insight = chosen?.data?.insight;
    if (chosen?.layer === 'insight' && insight?.state === 'pending')
      sendSignal({ kind: 'view', project_id: chosen.sourceProject, object: { kind: 'insight', id: insight.id, revision: insight.revision } }, chosen.signalToken);
  }, [chosen]);
  const detailProject = chosen?.sourceProject || readProject;
  const project = projects.find(row => row.id === projectId);
  useEffect(() => { setReviewing(false); setInbox(false); setSuggestions(null); setScene(''); setQ(''); setFilter(null); setSelection(null); setGroup(null); setLibraryMore(false); setMore(false); setMaintenance(null); setEditing(false); setBusy(false); setError(''); setConsolidation(null); setConsolidating(false); consolidationPending.current = null; }, [projectId]);
  useEffect(() => {
    if (documentId) { setLayer('note'); open('note', { document_id: documentId }); }
    return () => { drillGeneration.current += 1; };
  }, [projectId, documentId]);
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
      await libraryApi.consolidate(projectId);
      if (consolidationGate.isCurrent(request)) { setLibraryMore(false); setRefresh(value => value + 1); }
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
    libraryApi.inboxProjects({ signal: controller.signal }).then(result => {
      if (!controller.signal.aborted && currentScope.current === scope)
        setProjectSuggestions({ scope, items: result.items || [] });
    }).catch(reason => { if (!controller.signal.aborted && currentScope.current === scope) setError(reason.message); });
    return () => controller.abort();
  }, [scope, refresh]);
  async function open(entryLayer, row, targetProject = readProject, readonly = false) {
    const generation = ++drillGeneration.current, signalToken = signalEpoch();
    const identity = row.id || row.document_id;
    setSelection(null); setGroup(null); setLibraryMore(false); setMore(false); setMaintenance(null); setEditing(false); setError('');
    try {
      const data = await libraryApi.drill(targetProject, entryLayer, identity);
      if (currentScope.current === scope && generation === drillGeneration.current) {
        setSelection({ scope, sourceProject:targetProject, layer: entryLayer, entered: entryLayer, row, data, readonly, signalToken });
        libraryApi.opened(targetProject, entryLayer, identity);
      }
    } catch (reason) { if (currentScope.current === scope && generation === drillGeneration.current) setError(reason.message); }
  }
  async function selectDrill(options) {
    const selected = chosen;
    if (!selected) return;
    const generation = ++drillGeneration.current;
    setError('');
    try {
      const data = await libraryApi.drill(selected.sourceProject || readProject, selected.entered, selected.row.id || selected.row.document_id, options);
      if (currentScope.current === scope && generation === drillGeneration.current) {
        setSelection(value => value && value.scope === selected.scope
          && value.sourceProject === selected.sourceProject && value.entered === selected.entered
          && value.row === selected.row && value.data === selected.data ? { ...value, data } : value);
        if (options.document_id) libraryApi.opened(selected.sourceProject || readProject, 'document', options.document_id);
        setEditing(false); setMore(false); setMaintenance(null);
      }
    } catch (reason) { if (currentScope.current === scope && generation === drillGeneration.current) setError(reason.message); }
  }
  async function mutate(operation) {
    if (busy) return;
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
  const noteArchived = chosen?.data.note && archivedIds.has(chosen.data.note.document_id);
  const noteCooled = chosen?.data.note?.recall_state === 'cooled';
  const insight = chosen?.data.insight;
  const insightRecoverable = insight?.state === 'forgotten' || insight?.recall_state === 'cooled';
  const sourceProject = chosen?.data.source_project_id || detailProject;
  const readonlySource = chosen?.readonly || chosen?.data.readonly;
  const destination = projects.find(row => row.id === insight?.hint?.scope_hint && row.id !== detailProject);
  const original = chosen?.data.source || (chosen?.entered === 'source' ? chosen.row : null);
  function jump(next) {
    setEditing(false); setMore(false); setMaintenance(null);
    setSelection(value => value && { ...value, layer: next });
    const identity = next === 'insight' ? chosen?.data.insight?.id : chosen?.data.note?.document_id;
    if (identity && next !== chosen?.layer) libraryApi.opened(detailProject, next, identity);
  }
  const panelActions = !readonlySource && chosen?.layer === 'note' && chosen.data.note && !chosen.data.note.verified && !noteArchived
    ? <button type="button" disabled={busy} onClick={() => mutate(() => libraryApi.verify(detailProject, chosen.data.note))}>核对完成</button> : null;
  function clearReviewDrill() {
    drillGeneration.current += 1; setSelection(null); setError('');
  }
  return <div className={`library-page ${chosen || group || reviewing ? 'has-focus' : ''}`}>
    <aside className="library-scenes" aria-label="场景">
      <button type="button" aria-pressed={!inbox && !scene} onClick={() => { setInbox(false); setScene(''); setSelection(null); }}>全部</button>
      {(project?.scenes || []).map(name => <button type="button" key={name} aria-pressed={!inbox && scene === name} onClick={() => { setInbox(false); setScene(name); setSelection(null); }}>{name}</button>)}
      <div className="library-scene-divider"/>
      <button type="button" aria-pressed={inbox || projectId === 'inbox'} onClick={() => { setInbox(true); setScene(''); setFilter(null); setLayer('insight'); setSelection(null); setGroup(null); }}>收件箱 <span className="library-meta">{inboxCount ?? '—'}</span></button>
      <button type="button" aria-pressed={projectId === 'me'} onClick={() => onNavigate?.('library', { project_id: 'me' })}>我</button>
    </aside>
    {reviewing ? <SignalReviews key={projectId} projectId={projectId} reviews={reviews} onBack={() => { clearReviewDrill(); setReviewing(false); }}
      onOpen={object => open(object.kind === 'document' ? 'note' : 'insight', { id: object.id }, projectId)} onSelect={clearReviewDrill} drillError={error} hideEvidence={Boolean(chosen)} onChanged={() => setRefresh(value => value + 1)}/>
    : <main className="library-main">
      <LayerTabs value={layer} counts={counts} onChange={next => { setInbox(false); setLayer(next); setFilter(null); setSelection(null); }}>
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
      {readProject === 'inbox' && layer === 'insight' && projectSuggestions?.scope === scope && projectSuggestions.items.map(group =>
        <InboxProjectSuggestion key={`${scope}:${group.ids.join(':')}`} group={group} rows={visible?.insight?.items || []} scope={scope}
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
    {chosen && !(skillExport?.scope === scope && skillExport.sourceId === chosen.data.insight?.id) && <FocusPanel className="library-focus" title={labels[chosen.layer]} status={chosen.layer === 'note' && chosen.data.note ? noteArchived ? 'forgotten-user' : chosen.data.note.verified ? 'done' : 'unverified' : undefined} actions={panelActions} onClose={() => { setSelection(null); setEditing(false); }}>
      <Breadcrumb current={chosen.layer} onJump={jump}/>
      {chosen.data.documents?.length > 1 && <section aria-label="整理稿选择">{chosen.data.documents.map(row => <Row key={row.document_id} title={row.title} selected={chosen.data.note?.document_id === row.document_id} onOpen={() => selectDrill({ document_id: row.document_id })}/>)}</section>}
      {chosen.layer === 'source' && chosen.entered !== 'source' && chosen.data.sources?.length > 1 && <section aria-label="原件选择">{chosen.data.sources.map(row => <Row key={row.id} title={row.title} selected={chosen.data.source?.id === row.id} onOpen={() => selectDrill({ document_id: chosen.data.note?.document_id, source_id: row.id })}/>)}</section>}
      {chosen.layer === 'insight' && <>
        {chosen.entered !== 'insight' && <section className="library-grown"><h3>长出的认识</h3>{chosen.data.grown.map(row => <button type="button" key={row.id} onClick={() => open('insight', row)}>{row.text}</button>)}</section>}
        {insight && <>
          {editing ? <form className="library-edit" onSubmit={event => { event.preventDefault(); mutate(() => libraryApi.edit(detailProject, insight, text, conditions.split('\n').map(value => value.trim()).filter(Boolean))); }}>
            <label>认识<textarea aria-label="认识正文" value={text} onChange={event => setText(event.target.value)}/></label>
            <label>条件<textarea aria-label="适用条件" value={conditions} onChange={event => setConditions(event.target.value)}/></label>
            <button type="submit" disabled={busy}>保存</button><button type="button" onClick={() => setEditing(false)}>取消</button>
          </form> : <p className="library-insight-text">{insight.text}</p>}
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
            {['pending', 'active'].includes(insight.state) && <button type="button" disabled={busy} onClick={() => { setText(insight.text); setConditions((insight.conditions || []).join('\n')); setEditing(true); }}>编辑</button>}
            {insight.kind === 'candidate' && insight.state === 'forgotten' && <button type="button" disabled={busy} onClick={() => mutate(() => libraryApi.forget(detailProject, insight, false))}>捡回</button>}
            {insight.kind === 'recognition' && ['active', 'forgotten'].includes(insight.state) && <button type="button" disabled={busy} onClick={() => mutate(() => libraryApi.forget(detailProject, insight, !insightRecoverable))}>{insightRecoverable ? '恢复' : '遗忘'}</button>}
            {insight.kind === 'recognition' && <button type="button" aria-expanded={more} onClick={()=>setMore(value=>!value)}>更多</button>}
          </div>}
          {more && <div className="library-actions">{[['split','拆分'],['merge','合并'],['versions','版本'],['erase','永久删除']].filter(([key])=>!['split','merge'].includes(key)||insight.state==='active').map(([key,name])=><button key={key} type="button" onClick={()=>{setMaintenance(key);setMore(false);}}>{name}</button>)}
            {!readonlySource && insight.kind === 'recognition' && insight.state === 'active' && insight.conditions?.length > 0 && <button type="button" onClick={() => { setSkillExport({ scope, projectId: detailProject, scene: insight.scene, sourceId: insight.id }); setMore(false); }}>导出为 skill</button>}
          </div>}
          {maintenance && <InsightMaintenance key={`${projectId}:${insight.id}:${insight.revision}:${maintenance}`} projectId={detailProject} insight={insight} insights={visible?.insight?.items || []} action={maintenance} onDone={()=>{setSelection(null);setMore(false);setMaintenance(null);setRefresh(value=>value+1);}}/>}
          {insight.kind === 'recognition' && <InsightLinks key={`${detailProject}:${insight.id}`} projectId={detailProject} insightId={insight.id} insights={visible?.insight?.items || []} readonly={chosen.readonly} onOpen={row => open('insight', row, detailProject, chosen.readonly)} onReviewed={() => setRefresh(value => value + 1)}/>}
          <ConsolidationSuggestions key={`consolidation:${detailProject}:${insight.id}:${insight.revision}`} projectId={detailProject} insightId={insight.id} originals={insight.merged_from} readonly={chosen.readonly} onOpenDocument={(id,sourceProject)=>open('note',{document_id:id},sourceProject, chosen.readonly)} onReviewed={()=>{setSelection(null);setRefresh(value=>value+1);}}/>
          {insight.related?.length > 0 && <section className="library-grown"><h3>相关</h3>{insight.related.map(row => <button type="button" key={row.id} onClick={() => open('insight', row, detailProject, chosen.readonly)}>{row.text}</button>)}</section>}
        </>}
      </>}
      {chosen.layer === 'summary' && chosen.data.summary && <><p className="library-meta">{date(chosen.data.summary.created_at)}</p><h3 className="library-title">{chosen.data.summary.title}</h3><p className="library-summary-text">{chosen.data.summary.text}</p></>}
      {chosen.layer === 'note' && chosen.data.note && <>{readonlySource
        ? <MarkdownBody omitEmptyArtifacts evidence={chosen.data.note.facts.filter(fact => fact.evidence.quote === chosen.data.source?.window?.quote)} onEvidence={chosen.data.source?.window ? () => jump('source') : undefined}
          documentId={chosen.data.note.document_id} documentRevision={chosen.data.note.revision} commentSection={chosen.data.note.comment_section}>{chosen.data.note.markdown}</MarkdownBody>
        : <DocumentMarkdownEditor key={`${scope}:${detailProject}:${chosen.data.note.document_id}`} projectId={detailProject} documentId={chosen.data.note.document_id} document={chosen.data.note} disabled={busy}
          evidence={chosen.data.note.facts.filter(fact => fact.evidence.quote === chosen.data.source?.window?.quote)} onEvidence={chosen.data.source?.window ? () => jump('source') : undefined}
          readDocument={async () => (await libraryApi.drill(detailProject, 'note', chosen.data.note.document_id)).note}
          onSaved={document => { setSelection(value => value?.scope === scope && value.data === chosen.data ? { ...value, data: { ...value.data, note: { ...value.data.note, ...document, verified: true } } } : value); setRefresh(value => value + 1); }}/>
      }
        <DocumentPlacement key={`${detailProject}:${chosen.data.note.document_id}:${chosen.data.note.revision}`} projectId={detailProject}
          projects={projects} row={chosen.data.note} readonly={Boolean(readonlySource)} onError={setError}
          onChanged={() => { setSelection(null); setRefresh(value => value + 1); }}/>
        {!readonlySource && <div className="library-actions"><button type="button" disabled={busy} onClick={()=>mutate(async()=>{
          if (noteCooled && !noteArchived) await libraryApi.restoreRecall(detailProject, chosen.data.note);
          else await (noteArchived ? restoreDocument : archiveDocument)({projectId:detailProject,documentId:chosen.data.note.document_id,expectedRevision:chosen.data.note.revision});
          return {lifecycle:true};
        })}>{noteArchived || noteCooled ? '恢复' : '遗忘'}</button></div>}
      </>}
      {chosen.layer === 'source' && original && <><p className="library-meta">{kinds[original.kind] || '原件'}</p><h3 className="library-title">{original.title}</h3>
        {!readonlySource && !chosen.data.source_readonly && <OriginalPrivacy key={`${sourceProject}:${original.id}`} projectId={sourceProject} sourceId={original.id}/>}
        <OriginalText key={`${scope}:${original.id}:${chosen.data.note?.document_id || ''}`} projectId={sourceProject} original={original}/>
        {(original.url || original.download_url) && <ProductFileLink href={original.url || original.download_url} name={original.title} scopeKey={`${sourceProject}:${original.id}`}>打开原件</ProductFileLink>}
      </>}
      {layers.indexOf(chosen.layer) < 3 && (chosen.data[layers[layers.indexOf(chosen.layer) + 1]] || (chosen.layer === 'insight' && chosen.data.documents?.length > 1) || (chosen.layer === 'note' && chosen.data.sources?.length > 1)) && <button type="button" className="library-down" aria-label="下一层" onClick={() => jump(layers[layers.indexOf(chosen.layer) + 1])}>↓</button>}
    </FocusPanel>}
    {chosen && skillExport?.scope === scope && skillExport.sourceId === chosen.data.insight?.id && <SkillExportPanel key={`${scope}:${detailProject}:${skillExport.sourceId}`} projectId={skillExport.projectId} scene={skillExport.scene} sourceId={skillExport.sourceId} onClose={() => setSkillExport(null)}/>}
  </div>;
}
export default Library;
