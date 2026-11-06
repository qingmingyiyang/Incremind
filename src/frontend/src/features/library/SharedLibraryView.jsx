import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { LayerTabs, FilterBar, Row, FocusPanel, Breadcrumb, MarkdownBody, Icon } from '../../shared/ui';
import { InsightSource } from '../../shared/ui/InsightSource';
import { deviceIdentity, isDeviceIdentity, readDeviceCredential, userStorageKey } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { OriginalFiles, OriginalText } from './LibraryOriginals';
import { InsightReviewQueue, reviewRelationLabels } from './InsightReviewQueue';
import { sharedLibraryApi, sharedLibraryScope, sharedInsightEditorApi, sharedInsightMergeApi } from './sharedLibraryApi';
import { InsightEditor } from './LibraryInsightEditor';
import { DraftNavigationDialog, useDraftNavigation } from './DraftNavigation';
import { SharedSupersession } from './SharedSupersession';
import { InsightMergeReview } from './InsightMergeReview';
import { SharedActiveMerge } from './SharedActiveMerge';
import { LibraryMatch, LibrarySearchFeedback } from './LibrarySearchFeedback';

const layers = ['insight', 'summary', 'note', 'source'];
const labels = { insight: '认识', summary: '摘要', note: '整理稿', source: '原件' };
const reviewCapabilities = { edit: true, move: false, merge: false };
const dots = { pending: 'pending', active: 'done', stale: 'unverified', forgotten: 'forgotten' };
const noBusy = () => {};

export function SharedLibraryView({ projectId, projectScope, scopeLoading, scopeError, onScopesRetry,
  initialLayer = 'insight', initialFilter = null, initialReview = false, documentId, sourceId, insightId, onNavigationBlocked }) {
  const searchScopeId = useId();
  const identity = deviceIdentity();
  const [epoch, setEpoch] = useState(0), [layer, setLayer] = useState(layers.includes(initialLayer) ? initialLayer : 'insight');
  const [scene, setScene] = useState(''), [query, setQuery] = useState(''), [input, setInput] = useState(''), [composing, setComposing] = useState(false);
  const [filter, setFilter] = useState(initialFilter), [scenes, setScenes] = useState([]);
  const [loaded, setLoaded] = useState(null), [detail, setDetail] = useState(null), [error, setError] = useState('');
  const [refresh, setRefresh] = useState(0), [reviewing, setReviewing] = useState(false), [opening, setOpening] = useState(false);
  const [editing, setEditing] = useState(false), [editBusy, setEditBusy] = useState(false), [mutating, setMutating] = useState(false);
  const [replacement, setReplacement] = useState(null), [supersedeBusy, setSupersedeBusy] = useState(false);
  const [supersedeNotice, setSupersedeNotice] = useState(''), [journalCandidate, setJournalCandidate] = useState(null);
  const [completedSupersession, setCompletedSupersession] = useState(null);
  const [supersessionRefresh, setSupersessionRefresh] = useState(0);
  const [mergeDetail, setMergeDetail] = useState(null), [mergeBusy, setMergeBusy] = useState(false);
  const [pendingMergeBusy, setPendingMergeBusy] = useState(false), [mergeNotice, setMergeNotice] = useState('');
  const retry = useRef(null);
  const navigation = useRef(null);
  const canRead = !scopeLoading && !scopeError && projectScope?.scope_kind === 'shared' && projectScope.id === projectId
    && projectScope.membership === 'active' && projectScope.can_read_sources === true;
  const scopeKey = `shared:${projectId}:${epoch}:${identity.revision}:${canRead}:${projectScope?.role}:${scene}:${query}`;
  const gate = useRequestScope(scopeKey), scope = gate.scope;
  const owner = useRef(identity), busyState = useRef(null), editorFocus = useRef(null);
  busyState.current = { scope, dirty: false, busy: mutating || editBusy || supersedeBusy || mergeBusy || pendingMergeBusy,
    conflict: null, invalidated: !canRead };
  const busyGuard = useDraftNavigation({ gate, latest: busyState, owner, editor: editorFocus });
  const current = useRef({ scope, identity }); current.current = { scope, identity };
  const currentBusyGuard = useRef(busyGuard); currentBusyGuard.current = busyGuard;
  const requestNavigation = useCallback((action, options) => {
    if (!gate.isScopeCurrent(current.current.scope) || !isDeviceIdentity(current.current.identity)) return false;
    const complete = () => { setEditing(false); setReplacement(null); setMergeDetail(null); action(); };
    if (navigation.current) return navigation.current(complete, options);
    return currentBusyGuard.current.requestNavigation(complete, options);
  }, [gate]);
  const registerNavigation = useCallback(value => { navigation.current = value; }, []);
  useEffect(() => {
    if (composing || input === query) return;
    const timer = setTimeout(() => setQuery(input), 250);
    return () => clearTimeout(timer);
  }, [input, query, composing]);
  useEffect(() => {
    const report = { projectId, scope: scopeKey, identity, requestNavigation };
    onNavigationBlocked?.(report);
    return () => onNavigationBlocked?.({ ...report, disposed: true });
  }, [projectId, scopeKey, onNavigationBlocked, requestNavigation]);
  useEffect(() => {
    const changed = () => {
      setLoaded(null); setDetail(null); setReviewing(false); setError(''); setScene(''); setScenes([]);
      setInput(''); setQuery(''); setComposing(false); setEpoch(value => value + 1);
    };
    window.addEventListener('chriptmas-user-space', changed); window.addEventListener('chriptmas-device-required', changed);
    return () => { window.removeEventListener('chriptmas-user-space', changed); window.removeEventListener('chriptmas-device-required', changed); };
  }, []);
  useEffect(() => {
    busyGuard.clearNavigation();
    setLoaded(null); setDetail(null); setReviewing(false); setOpening(false); setEditing(false); setMutating(false); setError(''); retry.current = null;
    setReplacement(null); setSupersedeBusy(false); setJournalCandidate(null); setSupersedeNotice(''); setCompletedSupersession(null);
    setMergeDetail(null); setMergeNotice(''); setMergeBusy(false); setPendingMergeBusy(false);
  }, [scope]);
  useEffect(() => { if (initialReview && canRead && projectScope?.can_write) setReviewing(true); }, [initialReview, canRead, projectId, projectScope?.can_write]);
  useEffect(() => {
    setError('');
    if (!canRead) return;
    const controller = new AbortController(), token = gate.issue('list', scope);
    Promise.all(layers.map(name => sharedLibraryApi.list(name, projectId, { scene, q: query, signal: controller.signal })))
      .then(values => {
        const namespace = values[0].namespace;
        values.forEach(value => sharedLibraryScope(value, namespace));
        if (gate.isCurrent(token) && isDeviceIdentity(identity)) {
          setLoaded({ scope, namespace, scene, query, values: Object.fromEntries(layers.map((name, index) => [name, values[index]])) });
          if (!scene && !query) setScenes([...new Set(values[0].items.map(row => row.scene).filter(Boolean))]);
        }
      }).catch(reason => {
        if (!controller.signal.aborted && gate.isCurrent(token)) {
          retry.current = () => setRefresh(value => value + 1); setError(reason.message);
        }
      });
    return () => { controller.abort(); gate.invalidate('list'); gate.invalidate('detail'); };
  }, [scope, scene, query, refresh]);
  const data = loaded?.scope === scope && loaded.scene === scene && loaded.query === query ? loaded : null;
  const chosen = detail?.scope === scope && data?.namespace === detail.data.namespace ? detail : null;
  async function open(entered, id, options = {}, display = entered, permitted = false) {
    if ((editing || mutating || editBusy || supersedeBusy || mergeBusy || pendingMergeBusy) && !permitted) {
      requestNavigation(() => open(entered, id, options, display, true)); return;
    }
    if (!canRead || !data || !isDeviceIdentity(identity)) return;
    const token = gate.issue('detail', scope);
    setOpening(true); setError('');
    try {
      const { evidenceWindow, ...drillOptions } = options;
      let value = await sharedLibraryApi.drill(projectId, entered, id, drillOptions);
      sharedLibraryScope(value, data.namespace);
      if (entered === 'source' && evidenceWindow && value.source?.id === id)
        value = { ...value, source: { ...value.source, window: evidenceWindow } };
      if (gate.isCurrent(token) && isDeviceIdentity(identity)) setDetail({ scope, data: value, entered, id, layer: display });
    } catch (reason) {
      if (gate.isCurrent(token)) { retry.current = () => open(entered, id, options, display); setError(reason.message); }
    }
    finally { if (gate.isCurrent(token)) setOpening(false); }
  }
  useEffect(() => {
    if (!data) return;
    if (documentId) open('note', documentId);
    else if (sourceId) open('source', sourceId);
    else if (insightId) open('insight', insightId);
  }, [data?.namespace, documentId, sourceId, insightId]);
  function chooseLayer(next) {
    requestNavigation(() => { gate.invalidate('detail'); setDetail(null); setReviewing(false); setLayer(next); setFilter(null); });
  }
  function jump(next) {
    if (!chosen) return;
    if (chosen.data.needs_detail_read && next !== 'insight') { open('insight', chosen.data.insight.id, {}, next); return; }
    if (chosen.data[next]) requestNavigation(() => setDetail({ ...chosen, layer: next }));
  }
  const searching = composing || input !== query;
  const rows = (searching ? [] : data?.values[layer]?.items || []).filter(row => !filter || layer === 'insight' && row.state === filter
    || layer === 'note' && (row.archived ? 'archived' : row.verified ? 'verified' : 'unverified') === filter);
  const counts = Object.fromEntries(layers.map(name => [name, data ? data.values[name].items.length : null]));
  const closeDetail = () => requestNavigation(() => { gate.invalidate('detail'); setDetail(null); setOpening(false); });
  const listedOriginal = chosen?.entered === 'source' && data?.values.source.items.find(row => row.id === chosen.id);
  const source = chosen?.data.source || (listedOriginal
    ? { ...listedOriginal, window: listedOriginal.match?.part === 'text' ? listedOriginal.match : null } : null);
  const note = chosen?.data.note;
  const insight = chosen?.data.insight;
  const canWrite = Boolean(canRead && projectScope.can_write && chosen?.data.can_write);
  const canSupersede = Boolean(canRead && projectScope.can_write && data?.values.insight.can_write);
  const activeReplacement = canSupersede && replacement?.scope === scope && replacement.namespace === data.namespace ? replacement : null;
  const activeMergeDetail = canSupersede && mergeDetail?.scope === scope && mergeDetail.namespace === data.namespace ? mergeDetail.detail : null;
  const sharedMerge = useMemo(() => canSupersede ? {
    api: sharedInsightMergeApi(projectId, data.namespace, data.values.insight.authority),
    storage: userStorageKey(`shared-pending-merge:${readDeviceCredential()?.device?.device_id || ''}:${projectId}:${data.namespace}`),
  } : null, [scope, canSupersede, data?.namespace, data?.values.insight.authority]);
  function finishActiveMerge() {
    setMergeDetail(null); setDetail(null); setMergeNotice('已合并认识'); setRefresh(value => value + 1);
  }
  function openSupersession(value) {
    if (!canSupersede || journalCandidate || activeReplacement || activeMergeDetail || pendingMergeBusy
        || !gate.isScopeCurrent(scope) || !isDeviceIdentity(identity)
        || value.namespace !== data.namespace || !value.can_write || !value.can_edit_insight
        || value.insight?.kind !== 'candidate' || value.insight.state !== 'pending') return;
    setSupersedeNotice(''); setReplacement({ scope, namespace: data.namespace, detail: value });
  }
  function refreshSupersession() {
    setReplacement(null); setDetail(null); setRefresh(value => value + 1);
  }
  const supersessionStatus = canSupersede && (supersedeNotice || journalCandidate && !activeReplacement) && <div className="library-actions">
    {supersedeNotice && <span className="library-meta" role="status">{supersedeNotice}</span>}
    {journalCandidate && !activeReplacement && <button type="button" disabled={supersedeBusy || editing || mutating || editBusy}
      onClick={() => requestNavigation(() => setReplacement({ scope, namespace: data.namespace, checking: true }))}>核对取代</button>}
  </div>;
  const editorApi = useMemo(() => chosen?.data.insight ? sharedInsightEditorApi(projectId, chosen.data) : null, [projectId, chosen?.data]);
  const recoverable = insight?.state === 'forgotten' || insight?.recall_state === 'cooled';
  async function changeRecall() {
    if (!canWrite || mutating || editing || editBusy || !isDeviceIdentity(identity)) return;
    const token = gate.issue('maintenance', scope);
    if (!token) return;
    setMutating(true); setError('');
    try {
      const value = await sharedLibraryApi.forget(projectId, insight, !recoverable, chosen.data);
      if (!gate.isCurrent(token) || !isDeviceIdentity(identity)) return;
      setDetail({ ...chosen, data: value }); setRefresh(value => value + 1);
    } catch (failure) {
      if (!gate.isCurrent(token) || !isDeviceIdentity(identity)) return;
      if (failure.status === 409) {
        try {
          const value = await sharedLibraryApi.drill(projectId, 'insight', insight.id);
          sharedLibraryScope(value, chosen.data.namespace);
          if (gate.isCurrent(token) && isDeviceIdentity(identity)) setDetail({ ...chosen, data: value });
        } catch { /* 保留当前内容，下一次只读仍须核对同一实际范围。 */ }
      }
      if (gate.isCurrent(token) && isDeviceIdentity(identity)) { setError(failure.message); retry.current = () => open('insight', insight.id); }
    } finally { if (gate.isCurrent(token)) setMutating(false); }
  }
  return <div className="library-page">
    <aside className="library-scenes" aria-label="场景">
      <button type="button" aria-pressed={!scene} onClick={() => requestNavigation(() => setScene(''))}>全部</button>
      {canRead && scenes.map(name => <button type="button" key={name} aria-pressed={scene === name}
        onClick={() => requestNavigation(() => setScene(name))}>{name}</button>)}
    </aside>
    {reviewing && canSupersede ? <InsightReviewQueue key={`${scopeKey}:${scene}:${query}`} projectId={projectId}
      projects={[{ id: projectId, name: projectScope.name }]} scene={scene} query={query}
      api={sharedLibraryApi} scopeKind="shared" capabilities={reviewCapabilities} onBusy={noBusy}
      sharedMerge={sharedMerge}
      detailError={supersessionStatus} supersession={canSupersede ? { busy: supersedeBusy, selecting: Boolean(activeReplacement),
        lockedCandidate: journalCandidate, completed: completedSupersession, refresh: supersessionRefresh,
        onOpen: openSupersession } : null}
      onNavigationGuard={registerNavigation} onChanged={() => setRefresh(value => value + 1)}
      onBack={() => { setReviewing(false); setDetail(null); }}
      onOpenInsight={(row, owner) => { if (!owner || owner === projectId) open('insight', row.id); }} onOpenEvidence={(evidence, value) => {
        sharedLibraryScope(value, data.namespace); open('source', evidence.source_id, { document_id: evidence.document_id, evidenceWindow: evidence.window });
      }}/>
      : <main className="library-main">
        <LayerTabs value={layer} counts={counts} onChange={chooseLayer}>
          <div className="library-search-tools"><label className="library-search"><Icon name="search"/><input type="search" aria-label="搜索" aria-describedby={searchScopeId} placeholder={`搜索${labels[layer]}`}
            value={input} onCompositionStart={() => setComposing(true)} onCompositionEnd={event => { const value = event.target.value; setComposing(false); requestNavigation(() => setInput(value)); }}
            onChange={event => { const value = event.target.value; requestNavigation(() => setInput(value)); }}/></label></div>
        </LayerTabs>
        {scopeLoading ? <p role="status">读取共享资格中</p> : !canRead && <div className="library-error" role="alert">
          <span>{scopeError || '当前无法读取这个共享项目'}</span><button type="button" onClick={onScopesRetry}>重新读取</button></div>}
        {error && <div className="library-error" role="alert"><span>{error}</span><button type="button" onClick={() => retry.current?.()}>重新读取</button></div>}
        {supersessionStatus}
        {mergeNotice && <p className="library-meta" role="status">{mergeNotice}</p>}
        {sharedMerge && !chosen && <InsightMergeReview key={sharedMerge.storage} projectId={projectId} shared={sharedMerge}
          readonly={Boolean(activeReplacement || activeMergeDetail)} onBusy={setPendingMergeBusy} onChanged={() => setRefresh(value => value + 1)}/>}
        {canRead && <LibrarySearchFeedback id={searchScopeId} project={projectScope.name} scene={scene} layer={labels[layer]} query={input}
          loading={searching || !data} error={error} empty={rows.length === 0} onSearchSources={layer !== 'source' ? () => chooseLayer('source') : undefined}/>}
        {layer === 'insight' && <FilterBar value={filter} counts={data?.values.insight.counts} onChange={value => requestNavigation(() => setFilter(old => old === value ? null : value))}/>}
        {layer === 'note' && <FilterBar label="整理稿状态" value={filter} counts={data?.values.note.counts}
          filters={[{ key: 'unverified', label: '未核对', dot: 'unverified' }, { key: 'verified', label: '已核对', dot: 'done' }, { key: 'archived', label: '已遗忘', dot: 'forgotten' }]}
          onChange={value => requestNavigation(() => setFilter(old => old === value ? null : value))}/>}
        {layer === 'insight' && data?.values.insight.can_write && projectScope?.can_write
          && data.values.insight.items.some(row => row.state === 'pending') && <div className="library-confirm-entry">
            <button type="button" onClick={() => requestNavigation(() => { gate.invalidate('detail'); setDetail(null); setReviewing(true); })}>逐条确认<span>{data.values.insight.counts.pending}</span></button></div>}
        {rows.map(row => <Row key={row.id || row.document_id} title={layer === 'insight' ? row.text : row.title}
          label={row.match ? row.text || row.title : undefined}
          sub={row.match ? <LibraryMatch match={row.match}/> : layer === 'summary' ? row.summary : undefined}
          dot={layer === 'insight' ? dots[row.state] : layer === 'note' ? row.archived ? 'forgotten' : row.verified ? 'done' : 'unverified' : undefined}
          meta={row.match ? [projectScope.name, row.scene].filter(Boolean).join(' / ')
            : layer === 'insight' ? [row.by && `贡献 ${row.by}`, row.confirmed_by && `确认 ${row.confirmed_by}`].filter(Boolean).join(' · ') : undefined}
          onOpen={() => open(layer, row.id || row.document_id, row.match?.part === 'text' && layer === 'source' ? { evidenceWindow: row.match } : {})}/>) }
      </main>}
    {opening && !chosen && <p className="library-meta" role="status">读取详情中</p>}
    {chosen && !activeReplacement && !activeMergeDetail && <FocusPanel className="library-focus" title={labels[chosen.layer]} onClose={closeDetail}
      focusScope={`${scopeKey}:${chosen.layer}:${chosen.id}`}>
      <Breadcrumb levels={layers.filter(name => name === 'source' ? source : chosen.data[name])} current={chosen.layer} onJump={jump}/>
      {error && <div className="library-error" role="alert"><span>{error}</span><button type="button" onClick={() => retry.current?.()}>重新读取</button></div>}
      {chosen.layer === 'insight' && insight && <>
        {chosen.data.needs_detail_read && <div className="library-actions"><span role="status">已保存</span>
          <button type="button" disabled={mutating || opening || editing} onClick={() => open('insight', insight.id)}>读取依据</button></div>}
        {editing ? <InsightEditor key={`${chosen.data.namespace}:${scopeKey}:${insight.id}`} projectId={projectId}
          namespace={chosen.data.namespace} insight={insight} api={editorApi} disabled={!canWrite || mutating}
          onBusy={setEditBusy} onNavigationGuard={registerNavigation} onCancel={() => setEditing(false)}
          onAdopt={value => setDetail(previous => previous === chosen ? { ...previous, data: value } : previous)}
          onSaved={() => {
            gate.invalidate('detail'); setDetail({ ...chosen, data: editorApi.currentDetail() });
            setEditing(false); setRefresh(value => value + 1);
          }}/>
          : <p className="library-insight-text">{insight.text}</p>}
        {(insight.state === 'pending' || insight.conditions?.length > 0) && <div className="library-queue-conditions"><span>适用条件</span>{insight.conditions?.length > 0
          ? <ul className="library-conditions">{insight.conditions.map((value, index) => <li key={index}>{value}</li>)}</ul> : <p>未设条件</p>}</div>}
        <InsightSource source={insight.source}/>
        {!chosen.data.needs_detail_read && chosen.data.evidence?.map((evidence, index) => <section className="library-queue-evidence" key={`${evidence.source_id}:${index}`}>
          <header><span>{evidence.title}</span><button type="button" onClick={() => open('source', evidence.source_id, { document_id: evidence.document_id, evidenceWindow: evidence.window })}>打开原件</button></header>
          <blockquote>{evidence.window.pre}<mark>{evidence.window.quote}</mark>{evidence.window.post}</blockquote>
        </section>)}
        {insight.state === 'pending' && <><p className="library-meta">确认后在 {projectScope.name}{insight.scene ? ` / ${insight.scene}` : ''} 生效</p>
          {!insight.source && !chosen.data.evidence?.length && <p className="library-meta">暂无可核对的原句</p>}</>}
        {insight.hint?.target && <Row title={insight.hint.target.text} label={insight.hint.target.text} sub={reviewRelationLabels[insight.hint.relation] || '已有认识'}
          readOnly={Boolean(insight.hint.target.project_id && insight.hint.target.project_id !== projectId)} onOpen={() => open('insight', insight.hint.target.id)}/>}
        {!editing && canWrite && <div className="library-actions">
          {chosen.data.can_edit_insight && ['active', 'pending'].includes(insight.state) && <button type="button" disabled={mutating || opening || journalCandidate === insight.id}
            onClick={() => { gate.invalidate('detail'); setEditing(true); }}>编辑</button>}
          {canSupersede && !journalCandidate && chosen.data.can_edit_insight && insight.kind === 'candidate' && insight.state === 'pending'
            && <button type="button" disabled={mutating || opening || editBusy || pendingMergeBusy} onClick={() => openSupersession(chosen.data)}>
              <Icon name="supersedes" size={16}/>取代已有认识</button>}
          {sharedMerge && chosen.data.can_merge_insight === true && insight.kind === 'recognition' && insight.state === 'active'
            && <button type="button" disabled={mutating || opening || editBusy || pendingMergeBusy}
              onClick={() => { setMergeNotice(''); setMergeDetail({ scope, namespace: data.namespace, detail: chosen.data }); }}>合并认识</button>}
          {((insight.kind === 'recognition' && ['active', 'forgotten'].includes(insight.state))
            || insight.kind === 'candidate' && insight.state === 'forgotten')
            && <button type="button" disabled={mutating || opening || !Number.isSafeInteger(insight.recall_preference_revision)}
              onClick={changeRecall}>{recoverable ? '恢复' : '遗忘'}</button>}
        </div>}
        {sharedMerge && !reviewing && <InsightMergeReview key={sharedMerge.storage} projectId={projectId} shared={sharedMerge}
          detail={chosen.data} readonly={editing || mutating || opening} onBusy={setPendingMergeBusy}
          onChanged={result => { setDetail(null); setRefresh(value => value + 1);
            if (result.proposal?.state === 'approved') setMergeNotice('已合并 · 仍待确认'); }}
          onOpenTarget={row => open('insight', row.id)}/>}
        <p className="library-meta">{[insight.by && `贡献 ${insight.by}`, insight.confirmed_by && `确认 ${insight.confirmed_by}`].filter(Boolean).join(' · ')}</p>
      </>}
      {chosen.layer !== 'insight' && chosen.data.grown?.length > 0 && <section className="library-grown" aria-label="长出的认识">
        {chosen.data.grown.map(row => <Row key={row.id} title={row.text} onOpen={() => open('insight', row.id)}/>)}
      </section>}
      {chosen.data.documents?.length > 1 && <section aria-label="整理稿选择">{chosen.data.documents.map(row =>
        <Row key={row.document_id} title={row.title} selected={note?.document_id === row.document_id}
          onOpen={() => open(chosen.entered, chosen.id, { document_id: row.document_id }, 'note')}/>)}</section>}
      {chosen.layer === 'summary' && chosen.data.summary && <><h3 className="library-title">{chosen.data.summary.title}</h3><MarkdownBody>{chosen.data.summary.text}</MarkdownBody></>}
      {chosen.layer === 'note' && note && <><h3 className="library-title">{note.title}</h3><MarkdownBody
        documentId={note.document_id} documentRevision={note.revision} commentSection={note.comment_section}>{note.markdown}</MarkdownBody></>}
      {chosen.layer !== 'insight' && chosen.data.sources?.length > 1 && <section aria-label="原件选择">{chosen.data.sources.map(row =>
        <Row key={row.id} title={row.title} selected={source?.id === row.id} onOpen={() => open(chosen.entered, chosen.id,
          { document_id: note?.document_id, source_id: row.id }, 'source')}/>)}</section>}
      {chosen.layer === 'source' && source && <><h3 className="library-title">{source.title}</h3>
        <OriginalText key={`${scopeKey}:${chosen.data.namespace}:${source.id}:${JSON.stringify(source.window)}`} projectId={projectId} original={source}
          scopeKey={scopeKey} api={sharedLibraryApi} namespace={chosen.data.namespace} audioClips={false}/>
        <OriginalFiles key={`${scopeKey}:${chosen.data.namespace}:${source.id}`} projectId={projectId} original={source} scopeKey={`${scopeKey}:${chosen.data.namespace}:${source.id}`}
          images={sharedLibraryApi.images} namespace={chosen.data.namespace}/>
      </>}
    </FocusPanel>}
    {canSupersede && <SharedSupersession key={`${scopeKey}:${data.namespace}`} projectId={projectId} namespace={data.namespace}
      detail={activeReplacement?.detail} checking={activeReplacement?.checking} onBusy={setSupersedeBusy} onJournal={setJournalCandidate}
      onClose={() => setReplacement(null)} onNotice={setSupersedeNotice}
      onChanged={() => { refreshSupersession(); setSupersessionRefresh(value => value + 1); }}
      onApplied={candidateId => { setCompletedSupersession(candidateId); refreshSupersession(); }}/>}
    {sharedMerge && <SharedActiveMerge key={`${scopeKey}:${data.namespace}`} projectId={projectId} namespace={data.namespace}
      api={sharedMerge.api} detail={activeMergeDetail} onClose={() => setMergeDetail(null)} onDone={finishActiveMerge} onBusy={setMergeBusy}/>}
    <DraftNavigationDialog guard={busyGuard} busy={mutating || editBusy || supersedeBusy || mergeBusy || pendingMergeBusy} dirty={false} canSave error={error}
      onDiscard={() => busyGuard.finishNavigation(busyGuard.pendingNavigation.current)}/>
  </div>;
}
