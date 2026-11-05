import { ConsolidationSuggestions } from './ConsolidationSuggestions';
import { InsightLinks } from './InsightLinks';
import { useCallback, useEffect, useRef, useState } from 'react';
import { LayerTabs, FilterBar, Row, Breadcrumb, FocusPanel, Icon, MarkdownBody, CandidateHint } from '../../shared/ui';
import { OutcomeFreshness } from '../../shared/ui/OutcomeFreshness';
import { InsightSource } from '../../shared/ui/InsightSource';
import { sendSignal, signalEpoch } from '../../shared/signalsApi';
import { libraryApi } from './libraryApi';
import { SignalReviews, useSignalReviews } from './SignalReviews';
import { Gaps, useGaps } from './Gaps';
import { InboxFiling } from './InboxFiling';
import { ConversationShareInbox } from '../conversation-shares/ConversationShareInbox';
import { DocumentPlacement } from './DocumentPlacement';
import { InboxProjectSuggestion } from './InboxProjectSuggestion';
import { MemoPanel, InsightMaintenance } from './LibraryTools';
import { archiveDocument, restoreDocument, loadArchivedDocuments } from '../rebuild/libraryOverviewApi';
import { recognitionApi } from '../../shared/api/recognitionApi';
import { DocumentMarkdownEditor } from '../../shared/ui/DocumentMarkdownEditor';
import { SkillExportPanel } from './SkillExportPanel';
import { CitationUsage } from './CitationUsage';
import { InspirationDetails, InspirationExtraction } from './InspirationExtraction';
import { AutomaticInspirations } from './AutomaticInspirations';
import { deviceIdentity, isDeviceIdentity } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { formatLocalDate } from '../../shared/lib/time';
import { workbenchApi } from '../workbench/workbenchApi';
import { InsightReviewQueue } from './InsightReviewQueue';
import { InsightMergeReview } from './InsightMergeReview';
import { ProfileCards } from './ProfileCards';
import { OriginalFiles, OriginalText } from './LibraryOriginals';
import { SharedLibraryView } from './SharedLibraryView';
import { InsightEditor } from './LibraryInsightEditor';
import './Library.css';

const layers = ['insight', 'summary', 'note', 'source'];
const labels = { insight: '认识', summary: '摘要', note: '整理稿', source: '原件' };
const dots = { pending: 'pending', active: 'done', stale: 'unverified', forgotten: 'forgotten' };
const insightDot = row => row.state === 'forgotten' ? row.recall_by === 'auto' ? 'forgotten' : 'forgotten-user'
  : row.recall_state === 'cooled' ? 'cooled' : dots[row.state];
const kinds = { text: '文字', link: '链接', file: '文件', audio: '录音', image: '图片', video: '视频', conversation: '对话', other: '原件' };
import { OriginalPrivacy } from './OriginalPrivacy';

const date = value => value ? formatLocalDate(value) : '';
const evidenceWindowKey = evidence => JSON.stringify([evidence.document_id, evidence.source_id,
  evidence.window.pre, evidence.window.quote, evidence.window.post]);
function usageAfterSave(selection, layer, saved) {
  const previous = selection.data[layer], identity = layer === 'insight' ? 'id' : 'document_id';
  const direct = selection.entered === layer || layer === 'note' && selection.entered === 'summary';
  const ownsTarget = layer === 'insight' || (selection.row.document_id || selection.row.id) === previous?.document_id;
  if (!direct || !ownsTarget || previous?.[identity] !== saved[identity]
      || !Number.isInteger(previous?.revision) || !Number.isInteger(saved.revision)
      || previous.revision === saved.revision) return selection.data.used_by;
  return selection.data.used_by?.map(entry => ({ ...entry, changed: true }));
}

export function Library(props) {
  return props.projectScopeKind === 'shared' ? <SharedLibraryView {...props}/> : <PersonalLibrary {...props}/>;
}

function PersonalLibrary({ projectId = 'default', projects = [], initialLayer = 'insight', initialFilter = null, documentId, sourceId, insightId, inspirationJobId, onNavigationBlocked, onNavigate, onProjectCreated, serverConversationShares }) {
  const [layer, setLayer] = useState(layers.includes(initialLayer) ? initialLayer : 'insight'), [scene, setScene] = useState(''), [q, setQ] = useState('');
  const [composing, setComposing] = useState(false), [search, setSearch] = useState({ project: projectId, q: '' });
  const [filter, setFilter] = useState(initialFilter), [loaded, setLoaded] = useState(null), [refresh, setRefresh] = useState(0);
  const [inboxCount, setInboxCount] = useState(null);
  const [inbox, setInbox] = useState(false), [suggestions, setSuggestions] = useState(null);
  const [sharePanelHost, setSharePanelHost] = useState(null), [sharePanelOpen, setSharePanelOpen] = useState(false);
  const [projectSuggestions, setProjectSuggestions] = useState(null);
  const readProject = inbox ? 'inbox' : projectId;
  const [selection, setSelection] = useState(null), [error, setError] = useState(''), [mutating, setBusy] = useState(false);
  const [failedDrill, setFailedDrill] = useState(null), [userEpoch, setUserEpoch] = useState(0);
  const [placementNoticeScope, setPlacementNoticeScope] = useState(null);
  const [group, setGroup] = useState(null), [libraryMore, setLibraryMore] = useState(false), [constraintCount, setConstraintCount] = useState(null), [more, setMore] = useState(false), [maintenance, setMaintenance] = useState(null);
  const insightMoreTrigger = useRef(null);
  const [editing, setEditing] = useState(false), [editBusy, setEditBusy] = useState(false);
  const [extraction, setExtraction] = useState(null), [extractionBusy, setExtractionBusy] = useState(false);
  const [automatic, setAutomatic] = useState(null);
  const [noteEditorState, setNoteEditorState] = useState(null);
  const [reviewBusy, setReviewBusy] = useState(false);
  const [mergeBusy, setMergeBusy] = useState(false);
  const busy = mutating || editBusy || extractionBusy || reviewBusy || mergeBusy;
  const [skillExport, setSkillExport] = useState(null);
  const [consolidation, setConsolidation] = useState(null), [consolidating, setConsolidating] = useState(false);
  const [consolidationRead, setConsolidationRead] = useState(0);
  const [reviewing, setReviewing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const reviewNavigationGuard = useRef(null);
  const registerReviewNavigation = useCallback(request => { reviewNavigationGuard.current = request; }, []);
  const requestReviewNavigation = useCallback((action, options) => {
    const complete = () => { setEditing(false); action(); };
    if (reviewNavigationGuard.current) return reviewNavigationGuard.current(complete, options);
    complete(); return true;
  }, []);
  const reviews = useSignalReviews(projectId, refresh);
  const gaps = useGaps(projectId, refresh);
  const consolidationGate = useRequestScope(projectId), consolidationScope = consolidationGate.scope, consolidationPending = useRef(null);
  const scope = `${projectId}\0${inbox}\0${scene}\0${q}`;
  const extractionKey = `${scope}\0${filter}\0${userEpoch}`;
  const activeExtraction = extraction?.key === extractionKey && isDeviceIdentity(extraction.identity) ? extraction : null;
  const activeAutomatic = !group && !reviewing && !activeExtraction
    && automatic?.key === extractionKey && isDeviceIdentity(automatic.identity) ? automatic : null;
  const readingIdentity = deviceIdentity();
  useEffect(() => { setConfirming(false); }, [scope, userEpoch]);
  useEffect(() => { setExtraction(null); setAutomatic(null); }, [extractionKey]);
  useEffect(() => { if (group || reviewing) setAutomatic(null); }, [group, reviewing]);
  const drillGate = useRequestScope(`${scope}\0${filter}\0${userEpoch}`), drillScope = drillGate.scope;
  const drillError = drillGate.isCurrent(failedDrill?.request) ? failedDrill : null;
  const placementNotice = drillGate.isScopeCurrent(placementNoticeScope);
  const currentScope = useRef(scope), drillGeneration = useRef(0);
  currentScope.current = scope;
  const listScope = `${scope}\0${refresh}\0${userEpoch}`;
  const listGate = useRequestScope(`${listScope}\0${layer}`), listRequestScope = listGate.scope;
  const retryGate = useRequestScope(`${scope}\0${filter}\0${userEpoch}`), retryScope = retryGate.scope;
  const retryPending = useRef(new Map()), [retryRequests, setRetryRequests] = useState({});
  const retryKey = row => JSON.stringify([userEpoch, readProject, row.turn_id]);
  async function retryOriginal(row) {
    const key = retryKey(row);
    if (row.state !== 'failed' || !row.turn_id || retryPending.current.has(key)) return;
    const request = retryGate.issue(`original:${key}`, retryScope);
    if (!request) return;
    retryPending.current.set(key, request); setRetryRequests(current => ({ ...current, [key]: request })); setError('');
    try {
      await workbenchApi.retry(readProject, row.turn_id);
      if (retryGate.isCurrent(request)) setRefresh(value => value + 1);
    } catch (reason) {
      if (retryGate.isCurrent(request)) setError(reason.message);
    } finally {
      if (retryPending.current.get(key) === request) retryPending.current.delete(key);
      setRetryRequests(current => {
        if (current[key] !== request) return current;
        const next = { ...current }; delete next[key]; return next;
      });
    }
  }
  useEffect(() => {
    if (composing || (search.project === projectId && search.q === q)) return;
    const timer = setTimeout(() => setSearch({ project: projectId, q }), 250);
    return () => clearTimeout(timer);
  }, [projectId, q, composing, search]);
  useEffect(() => {
    const changed = () => {
      drillGate.invalidate('detail'); drillGeneration.current += 1;
      setFailedDrill(null); setSelection(null); setExtraction(null); setAutomatic(null); setExtractionBusy(false); setError(''); setUserEpoch(value => value + 1);
    };
    window.addEventListener('chriptmas-user-space', changed);
    window.addEventListener('chriptmas-device-required', changed);
    return () => {
      window.removeEventListener('chriptmas-user-space', changed);
      window.removeEventListener('chriptmas-device-required', changed);
    };
  }, []);
  const visible = loaded?.scope === listScope ? loaded.data : null;
  useEffect(() => {
    if (layer !== 'source' || !visible?.source?.items.some(row => row.state === 'processing')) return;
    const timer = setTimeout(() => setRefresh(value => value + 1), 1500);
    return () => clearTimeout(timer);
  }, [layer, visible]);
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
    else if (insightId) { setLayer('insight'); open('insight', { id: insightId }); }
    return () => { drillGeneration.current += 1; };
  }, [projectId, documentId, sourceId, insightId, userEpoch]);
  useEffect(() => {
    if (inspirationJobId) setAutomatic({ key: extractionKey, id: inspirationJobId, identity: deviceIdentity() });
  }, [projectId, inspirationJobId]);
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
    setError('');
    if (composing || search.project !== projectId || search.q !== q) return;
    const controller = new AbortController();
    const request = listGate.issue('list', listRequestScope);
    libraryApi.list(layer, readProject, { scene, q: search.q, signal: controller.signal })
      .then(value => {
        if (!controller.signal.aborted && listGate.isCurrent(request)) setLoaded(previous => ({ scope: listScope,
          data: { ...(previous?.scope === listScope ? previous.data : {}), [layer]: value } }));
      })
      .catch(reason => { if (!controller.signal.aborted && listGate.isCurrent(request)) setError(reason.message); });
    return () => controller.abort();
  }, [listScope, layer, composing, search, listRequestScope]);
  useEffect(() => {
    if (layer !== 'note' || filter !== 'archived' || composing || search.project !== projectId || search.q !== q) return;
    let current = true;
    const request = listGate.issue('archives', listRequestScope);
    loadArchivedDocuments({ projectId: readProject }).then(value => {
      if (current && listGate.isCurrent(request)) setLoaded(previous => ({ scope: listScope,
        data: { ...(previous?.scope === listScope ? previous.data : {}), archived: value.items || [] } }));
    }).catch(reason => { if (current && listGate.isCurrent(request)) setError(reason.message); });
    return () => { current = false; };
  }, [listScope, layer, filter, composing, search, listRequestScope]);
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
    if (readProject !== 'inbox' || layer !== 'insight' || composing || search.project !== projectId || search.q !== q) return;
    const controller = new AbortController();
    const request = listGate.issue('project-suggestions', listRequestScope);
    Promise.all([libraryApi.inboxProjects({ signal: controller.signal }),
      libraryApi.list('insight', 'inbox', { signal: controller.signal })]).then(([result, originals]) => {
      if (!controller.signal.aborted && listGate.isCurrent(request))
        setProjectSuggestions({ scope, items: result.items || [], rows: originals.items || [] });
    }).catch(reason => { if (!controller.signal.aborted && listGate.isCurrent(request)) setError(reason.message); });
    return () => controller.abort();
  }, [listScope, layer, composing, search, listRequestScope]);
  async function documentArchived(targetProject, data, readonly) {
    if (!data.note || data.readonly || readonly) return false;
    return data.note.archived === true;
  }
  async function open(entryLayer, row, targetProject = readProject, readonly = false, originMeta = null, permitted = false) {
    if (editing && !permitted) { requestReviewNavigation(() => open(entryLayer, row, targetProject, readonly, originMeta, true)); return; }
    const request = drillGate.issue('detail', drillScope);
    if (!request) return;
    const generation = ++drillGeneration.current, signalToken = signalEpoch();
    const identity = row.id || row.document_id;
    setSelection(null); setExtraction(null); setAutomatic(null); setGroup(null); setLibraryMore(false); setMore(false); setMaintenance(null); setEditing(false); setError(''); setFailedDrill(null);
    try {
      const data = await libraryApi.drill(targetProject, entryLayer, identity);
      if (originMeta?.namespace && data.namespace !== originMeta.namespace) throw new Error('来源已变化 · 重新读取');
      const archived = await documentArchived(targetProject, data, readonly);
      if (drillGate.isCurrent(request) && generation === drillGeneration.current) {
        setSelection({ scope, sourceProject:targetProject, layer: entryLayer, entered: entryLayer, row, data, archived,
          readonly: readonly || data.can_write === false || Boolean(originMeta?.readonly), originMeta, signalToken });
        libraryApi.opened(targetProject, entryLayer, identity);
        return data;
      }
    } catch (reason) {
      if (drillGate.isCurrent(request) && generation === drillGeneration.current)
        setFailedDrill({ request, message: reason.message, retry: () => open(entryLayer, row, targetProject, readonly, originMeta) });
    }
  }
  async function selectDrill(options, view = {}, permitted = false) {
    if (editing && !permitted) { requestReviewNavigation(() => selectDrill(options, view, true)); return; }
    const selected = chosen;
    if (!selected) return;
    const request = drillGate.issue('detail', drillScope);
    if (!request) return;
    const generation = ++drillGeneration.current;
    setError(''); setFailedDrill(null);
    try {
      let data = await libraryApi.drill(selected.sourceProject || readProject, selected.entered, selected.row.id || selected.row.document_id, options);
      const targetProject = data.documents?.find(row => row.document_id === options.document_id)?.project_id
        || data.source_project_id || selected.sourceProject || readProject;
      if (view.evidence) {
        const evidence = data.evidence?.find(row => evidenceWindowKey(row) === evidenceWindowKey(view.evidence));
        if (data.source?.id !== view.evidence.source_id || !evidence || targetProject !== view.projectId) {
          const changed = new Error('内容已变化 · 重新打开');
          changed.evidenceChanged = true;
          throw changed;
        }
        data = { ...data, source_project_id: targetProject, source: { ...data.source, window: evidence.window } };
      }
      const archived = await documentArchived(selected.sourceProject || readProject, data, selected.readonly);
      if (drillGate.isCurrent(request) && generation === drillGeneration.current) {
        setSelection(value => value && value.scope === selected.scope
          && value.sourceProject === selected.sourceProject && value.entered === selected.entered
          && value.row === selected.row && value.data === selected.data ? { ...value, data, archived,
            layer: view.layer || value.layer, evidenceKey: view.evidence ? evidenceWindowKey(view.evidence) : undefined } : value);
        if (options.document_id) libraryApi.opened(targetProject, 'document', options.document_id);
        setEditing(false); setMore(false); setMaintenance(null);
      }
    } catch (reason) {
      if (drillGate.isCurrent(request) && generation === drillGeneration.current)
        setFailedDrill({ request, message: reason.message, retry: () => selectDrill(options, reason.evidenceChanged ? {} : view) });
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
        return {...selected.data,readonly:true,insight_readonly:true,can_edit_insight:false,insight:{...selected.data.insight,text:snapshot.content,
          revision:current.recognition_revision,state:snapshot.state,conditions:snapshot.conditions,
          source_count:new Set([...snapshot.source_experience_ids,...snapshot.source_recognition_ids]).size}};
      }),
    ]);
    const archived = await documentArchived(targetProject, data, selected.readonly);
    if (!drillGate.isCurrent(request) || generation!==drillGeneration.current) return null;
    setSelection(value=>value===selected ? {...value,data,archived,readonly:value.readonly || data.can_write === false || Boolean(data.insight_readonly)} : value);
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
  const counts = Object.fromEntries(layers.map(key => [key, !visible?.[key] ? null : key === 'insight' && visible[key].counts
    ? Object.values(visible[key].counts).reduce((sum, value) => sum + value, 0) : visible[key].items.filter(row => key !== 'source' || !row.state).length]));
  const archivedIds = new Set((visible?.archived || []).map(row => row.document_id));
  const noteState = row => (row.archived ?? archivedIds.has(row.document_id)) ? 'archived' : row.verified ? 'verified' : 'unverified';
  const noteCounts = visible?.note?.counts || Object.fromEntries(['unverified', 'verified', 'archived'].map(key => [key, (visible?.note?.items || []).filter(row => noteState(row) === key).length]));
  const rows = (visible?.[layer]?.items || []).filter(row => filter === null || (layer === 'insight' ? row.state === filter : layer !== 'note' || noteState(row) === filter));
  const noteArchived = chosen?.data.note && chosen.archived;
  const noteCooled = chosen?.data.note?.recall_state === 'cooled';
  const insight = chosen?.data.insight;
  const insightProject = chosen?.data.insight_project_id || detailProject;
  const canEditInsight = Boolean(chosen?.data.can_edit_insight && !chosen.readonly
    && chosen.data.insight_project_id === insightProject);
  const pendingInsight = chosen?.layer === 'insight' && insight?.state === 'pending';
  const insightRecoverable = insight?.state === 'forgotten' || insight?.recall_state === 'cooled';
  const sourceProject = chosen?.data.source_project_id || detailProject;
  const readonlySource = chosen?.readonly || chosen?.data.readonly;
  const destination = projects.find(row => row.id === insight?.hint?.scope_hint && row.id !== detailProject);
  const original = chosen?.data.source || (chosen?.entered === 'source' ? chosen.row : null);
  const focusId = { insight: insight?.id, summary: chosen?.data.summary?.document_id,
    note: chosen?.data.note?.document_id, source: original?.id }[chosen?.layer] || chosen?.row?.id || chosen?.row?.document_id;
  const focusScope = chosen ? JSON.stringify([chosen.layer === 'source' ? sourceProject : detailProject, chosen.layer, focusId]) : undefined;
  const editorScope = `${extractionKey}\0${detailProject}\0${chosen?.data.note?.document_id || ''}`;
  const noteEditorChanged = useCallback(value => setNoteEditorState({ scope: editorScope, ...value }), [editorScope]);
  const unsavedNote = chosen?.layer === 'note' && !readonlySource && !noteArchived
    && (noteEditorState?.scope !== editorScope || noteEditorState.dirty || noteEditorState.saving || noteEditorState.conflict);
  useEffect(() => {
    if (!onNavigationBlocked) return;
    const report = { scope: drillScope, identity: readingIdentity, projectId, blocked: Boolean(busy || editing || unsavedNote),
      nonReviewBlocked: Boolean(mutating || editBusy || extractionBusy || mergeBusy || editing || unsavedNote),
      requestNavigation: confirming || editing ? requestReviewNavigation : undefined };
    onNavigationBlocked(report);
    return () => onNavigationBlocked({ ...report, disposed: true });
  }, [onNavigationBlocked, drillScope, projectId, busy, mutating, editBusy, extractionBusy, mergeBusy, editing, unsavedNote, confirming, requestReviewNavigation]);
  const documentChoices = chosen?.data.documents?.length > 1 && <section aria-label="整理稿选择">{chosen.data.documents.map(row => <Row key={row.document_id} title={row.title} selected={chosen.data.note?.document_id === row.document_id} onOpen={() => selectDrill({ document_id: row.document_id })}/>)}</section>;
  const grownChoices = chosen?.layer === 'insight' && chosen.entered !== 'insight' && <section className="library-grown"><h3>长出的认识</h3>{chosen.data.grown.map(row => <button type="button" key={row.id} onClick={() => open('insight', row)}>{row.text}</button>)}</section>;
  const insightMetadata = insight && <><p className="library-meta">{[insight.scene, `${insight.source_count} 源`].filter(Boolean).join(' · ')}</p>
    {insight.state === 'pending' && <InsightSource source={insight.source} onReference={onNavigate && !editing && !busy
      ? reference => requestReviewNavigation(() => onNavigate('workbench', reference)) : undefined}/>}
    {insight.conditions?.length > 0 && <ul className="library-conditions">{insight.conditions.map(value => <li key={value}>{value}</li>)}</ul>}</>;
  function openEvidence(evidence) {
    const project = chosen.data.documents?.find(row => row.document_id === evidence.document_id)?.project_id
      || chosen.data.source_project_id || detailProject;
    selectDrill({ document_id: evidence.document_id, source_id: evidence.source_id }, { layer: 'source', evidence, projectId: project });
  }
  function jump(next, permitted = false) {
    if (editing && !permitted) { requestReviewNavigation(() => jump(next, true)); return; }
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
  function openCitationUsage(entry) {
    if (!chosen || currentScope.current !== scope || !drillGate.isScopeCurrent(drillScope)) return;
    clearReviewDrill(); setEditing(false); setMore(false); setMaintenance(null);
    onNavigate?.('workbench', { project_id: detailProject, thread_id: entry.thread_id, turn_id: entry.turn_id });
  }
  function startInspiration(initialDocumentId, targetProject = readProject) {
    if (busy || editing || unsavedNote || !isDeviceIdentity(readingIdentity)
      || currentScope.current !== scope || !drillGate.isScopeCurrent(drillScope)) return;
    setLibraryMore(false); setGroup(null); setReviewing(false); setSkillExport(null); setAutomatic(null);
    setExtraction({ key: extractionKey, projectId: targetProject, scene: scene || null,
      initialDocumentId, identity: readingIdentity });
  }
  function openInspirationDocument(evidence, targetProject, readonly = false) {
    if (!isDeviceIdentity(readingIdentity) || currentScope.current !== scope || !drillGate.isScopeCurrent(drillScope)) return;
    open('note', { document_id: evidence.document_id }, targetProject, readonly);
  }
  const retryDrill = drillError && <div className="library-error" role="alert"><span>{drillError.message}</span><button type="button" onClick={() => { if (drillGate.isCurrent(drillError.request)) drillError.retry(); }}>重试</button></div>;
  return <div ref={setSharePanelHost} className={`library-page ${chosen || group || reviewing || activeExtraction || activeAutomatic || sharePanelOpen ? 'has-focus' : ''}`}>
    <aside className="library-scenes" aria-label="场景">
      <button type="button" aria-pressed={!inbox && !scene} onClick={() => requestReviewNavigation(() => { setInbox(false); setScene(''); setSelection(null); })}>全部</button>
      {(project?.scenes || []).map(name => <button type="button" key={name} aria-pressed={!inbox && scene === name} onClick={() => requestReviewNavigation(() => { setInbox(false); setScene(name); setSelection(null); })}>{name}</button>)}
      <div className="library-scene-divider"/>
      <button type="button" aria-pressed={inbox || projectId === 'inbox'} onClick={() => requestReviewNavigation(() => { setInbox(true); setScene(''); setFilter(null); setLayer('insight'); setSelection(null); setGroup(null); })}>收件箱 <span className="library-meta">{inboxCount ?? '—'}</span></button>
      <button type="button" aria-pressed={projectId === 'me'} onClick={() => requestReviewNavigation(() => onNavigate?.('library', { project_id: 'me' }))}>我</button>
    </aside>
    {confirming ? <InsightReviewQueue key={`${scope}:${userEpoch}`} projectId={readProject} projects={projects} scene={scene} query={q}
      onNavigationGuard={registerReviewNavigation}
      onReference={onNavigate ? reference => onNavigate('workbench', reference) : undefined}
      detailError={!chosen ? retryDrill : null}
      onBack={() => { setConfirming(false); setSelection(null); }} onBusy={setReviewBusy} onChanged={() => setRefresh(value => value + 1)}
      onOpenInsight={(row, ownerProject) => open('insight', row, ownerProject, true)}
      onOpenEvidence={(evidence, detail) => open('source', { id: evidence.source_id }, detail.documents?.find(row => row.document_id === evidence.document_id)?.project_id || detail.source_project_id || readProject)}/>
    : reviewing ? <SignalReviews key={projectId} projectId={projectId} reviews={reviews} onBack={() => { clearReviewDrill(); setReviewing(false); }}
      onOpen={object => open(object.kind === 'document' ? 'note' : 'insight', { id: object.id }, projectId)} onSelect={clearReviewDrill} drillError={drillError?.message || error} hideEvidence={Boolean(chosen)} onChanged={() => setRefresh(value => value + 1)}/>
    : <main className="library-main">
      <LayerTabs value={layer} counts={counts} onChange={next => requestReviewNavigation(() => { clearReviewDrill(); setInbox(false); setLayer(next); setFilter(null); })}>
        <div className="library-search-tools"><label className="library-search"><Icon name="search"/><input type="search" aria-label="搜索" value={q}
          onCompositionStart={() => setComposing(true)} onCompositionEnd={event => { const value = event.currentTarget.value; setComposing(false); requestReviewNavigation(() => { setQ(value); setSelection(null); }); }}
          onChange={event => { const value = event.target.value; requestReviewNavigation(() => { setQ(value); setSelection(null); }); }}/></label>
          <div className="library-more" onKeyDown={event => { if (event.key === 'Escape') { setLibraryMore(false); event.currentTarget.querySelector('button')?.focus(); } }}>
            <button type="button" className="library-more-trigger" aria-label="资料库更多" aria-haspopup="menu" aria-expanded={libraryMore} onClick={() => { setLibraryMore(value => !value); if (!libraryMore) { setConsolidationRead(value => value + 1); reviews.reload(); gaps.reload(); } }}>⋯</button>
            {libraryMore && <div className="library-more-menu" role="menu" aria-label="资料库更多">
              <button type="button" role="menuitem" disabled={busy || editing || Boolean(unsavedNote)} onClick={() => startInspiration()}>提炼灵感</button>
              <button type="button" role="menuitem" title="满10次自动整理" disabled={consolidating || consolidation?.project !== projectId || consolidation?.limit}
                onClick={consolidateNow}>现在整理 <span>{consolidation?.project === projectId ? consolidation.score : '—'}/10</span></button>
              <button type="button" role="menuitem" onClick={() => { setLibraryMore(false); onNavigate?.('settings', { project_id: projectId, section: 'project', expand_project: projectId }); }}>约束 <span>{constraintCount ?? '—'}</span><Icon name="open" size={14}/></button>
              <button type="button" role="menuitem" onClick={() => requestReviewNavigation(() => { setLibraryMore(false); setSelection(null); setGroup('memo'); })}>备忘</button>
              {reviews.items.length > 0 && <button type="button" role="menuitem" onClick={() => requestReviewNavigation(() => { clearReviewDrill(); setLibraryMore(false); setGroup(null); setReviewing(true); })}>纠偏 <span>{reviews.items.length}</span></button>}
              {gaps.items.length > 0 && <button type="button" role="menuitem" onClick={() => requestReviewNavigation(() => { setLibraryMore(false); setSelection(null); setReviewing(false); setGroup('gaps'); gaps.reload(); })}>待补 <span>{gaps.items.length}</span></button>}
              {gaps.error && <div className="library-error" role="alert"><span>{gaps.error}</span><button type="button" aria-label="重试待补" onClick={gaps.reload}>重试</button></div>}
              {reviews.error && <div className="library-error" role="alert"><span>{reviews.error}</span><button type="button" aria-label="重试纠偏" onClick={reviews.reload}>重试</button></div>}
            </div>}
          </div>
        </div>
      </LayerTabs>
      {layer === 'insight' && (visible?.insight?.counts?.pending || 0) > 0 && <div className="library-confirm-entry"><button type="button" disabled={busy || editing || Boolean(unsavedNote)} onClick={() => { clearReviewDrill(); setGroup(null); setReviewing(false); setConfirming(true); }}>逐条确认<span>{visible.insight.counts.pending}</span></button></div>}
      {layer === 'note' && <FilterBar label="整理稿状态" filters={[{ key: 'unverified', label: '未核对', dot: 'unverified' }, { key: 'verified', label: '已核对', dot: 'done' }, { key: 'archived', label: '已遗忘', dot: 'forgotten-user' }]} value={filter} counts={noteCounts} onChange={value => requestReviewNavigation(() => setFilter(old => old === value ? null : value))}/> }
      {layer === 'insight' && <FilterBar value={filter} counts={visible?.insight?.counts || {}} onChange={value => requestReviewNavigation(() => setFilter(old => old === value ? null : value))}/>}
      {error && <div className="library-error" role="alert"><span>{error}</span><button type="button" onClick={() => setRefresh(value => value + 1)}>重试</button></div>}
      {placementNotice && <div className="library-error" role="status">已移动 · 认识提炼未完成</div>}
      {!chosen && retryDrill}
      {serverConversationShares && readProject === 'inbox' && layer === 'insight' && <ConversationShareInbox
        projectId={projectId} projects={projects} panelHost={sharePanelHost} onDetailChange={setSharePanelOpen}
        panelAllowed={!chosen && !group && !activeExtraction && !activeAutomatic}
        openingDisabled={busy || Boolean(unsavedNote)}
        requestOpen={action => {
          if (!busy && !unsavedNote) requestReviewNavigation(() => {
            clearReviewDrill(); setGroup(null); setLibraryMore(false); setAutomatic(null); setSkillExport(null); action();
          });
        }} onNavigate={onNavigate} onFiled={() => setRefresh(value => value + 1)}/>}
      {readProject === 'inbox' && layer === 'insight' && projectSuggestions?.scope === scope && projectSuggestions.items.map(group =>
        <InboxProjectSuggestion key={`${scope}:${group.ids.join(':')}`} group={group} rows={projectSuggestions.rows} scope={scope}
          onProjectCreated={onProjectCreated} onChanged={() => { setSelection(null); setRefresh(value => value + 1); }}/>) }
      {projectId === 'me' && !inbox && layer === 'insight' && visible?.insight ? <ProfileCards items={rows} busy={busy || editing}
        onOpen={row => open('insight', row)} onEdit={async row => { const data = await open('insight', row); if (data?.can_edit_insight && data.insight_project_id === readProject) setEditing(true); }}
        onReview={(row, action) => { setSelection(null); mutate(() => libraryApi.review(readProject, row, action)); }}
        onForget={(row, forgotten) => { setSelection(null); mutate(() => libraryApi.forget(readProject, row, forgotten)); }}
        onAdd={onNavigate ? name => onNavigate('workbench', { project_id: 'me', compose: { intent: 'remember', scene: name === '关于我' || name === '规律' ? undefined : name } }) : undefined}/>
      : <div className="library-rows" aria-label={labels[layer]}>
        {rows.map(row => <Row key={row.id || row.document_id} dot={layer === 'source' && row.state ? row.state : layer === 'insight' ? insightDot(row) : layer === 'note' && (noteState(row) === 'archived' || row.recall_state === 'forgotten') ? 'forgotten-user' : row.recall_state === 'cooled' ? 'cooled' : layer === 'source' || row.verified ? 'done' : 'unverified'}
          title={row.pattern || (layer === 'insight' && row.state === 'pending' && row.hint) ? <>{row.pattern && <Icon name="pattern" size={14}/>} {row.text || row.title}{row.state === 'pending' && <CandidateHint relation={row.hint?.relation} commentSource={row.comment_source}/>}</> : layer === 'note' ? <>{row.text || row.title}{' '}<OutcomeFreshness stale={row.stale} freshCount={row.fresh_count} verified={row.changes_verified}/></> : row.text || row.title} sub={layer === 'summary' ? row.summary : undefined}
          meta={layer === 'insight' ? [row.scene, `${row.source_count} 源`].filter(Boolean).join(' · ') : [row.filing ? '已移到 #' + (projects.find(project=>project.id===row.filing.target_project_id)?.name || row.filing.target_project_id) : layer === 'source' ? kinds[row.kind] || '原件' : layer === 'note' && noteState(row) === 'archived' ? '已遗忘' : row.verified ? '已核对' : '未核对', date(row.created_at)].filter(Boolean).join(' · ')}
          selected={chosen && (Boolean(row.id) && chosen.data.insight?.id === row.id || Boolean(row.document_id) && chosen.data.note?.document_id === row.document_id)} onOpen={() => open(layer, row)} trailing={layer === 'source' && row.state ? <button type="button" disabled={row.state !== 'failed' || Boolean(retryRequests[retryKey(row)])} onClick={() => retryOriginal(row)}>重试</button> : inbox && projectId !== 'inbox' && layer === 'insight' && ['pending', 'active'].includes(row.state) ? <InboxFiling key={`${scope}:${row.id}:${row.revision}`} row={row} targetProjectId={projectId} scenes={project?.scenes || []} suggestion={suggestions?.project === projectId ? suggestions.values[row.id] : null} onFiled={() => { setSelection(null); setError(''); setRefresh(value => value + 1); }} onError={setError}/> : undefined}/>)}
      </div>}
    </main>}
    {group === 'gaps' && <Gaps key={projectId} projectId={projectId} gaps={gaps} onClose={() => setGroup(null)} onOpen={row => onNavigate?.('workbench', { project_id: projectId, compose: { scene: row.scene, intent: 'remember' } })}/>}
    {group === 'memo' && <MemoPanel key={projectId} projectId={projectId} onClose={()=>setGroup(null)}/>}
    {activeExtraction && <InspirationExtraction key={extractionKey} scopeKey={extractionKey} projectId={activeExtraction.projectId}
      scene={activeExtraction.scene} initialDocumentId={activeExtraction.initialDocumentId} onClose={() => setExtraction(null)}
      onBusyChange={setExtractionBusy} onGenerated={() => setRefresh(value => value + 1)}
      onOpenInsight={row => open('insight', row, activeExtraction.projectId)}
      onOpenDocument={evidence => openInspirationDocument(evidence, activeExtraction.projectId)}/>}
    {activeAutomatic && <AutomaticInspirations key={`${extractionKey}:${activeAutomatic.id}`} scopeKey={extractionKey}
      projectId={projectId} jobId={activeAutomatic.id} onClose={() => setAutomatic(null)}
      onRead={() => window.dispatchEvent(new Event('chriptmas-inspirations-read'))}
      onOpenInsight={row => { setAutomatic(null); open('insight', row, projectId); }}
      onOpenDocument={evidence => { setAutomatic(null); openInspirationDocument(evidence, projectId); }}/>}
    {chosen && !activeExtraction && !activeAutomatic && !(skillExport?.scope === scope && skillExport.sourceId === chosen.data.insight?.id) && <FocusPanel className="library-focus" title={labels[chosen.layer]} focusScope={focusScope} status={chosen.layer === 'note' && chosen.data.note ? noteArchived ? 'forgotten-user' : chosen.data.note.verified ? 'done' : 'unverified' : undefined} actions={panelActions} onClose={() => requestReviewNavigation(() => { clearReviewDrill(); setEditing(false); })}>
      <Breadcrumb current={chosen.layer} onJump={jump}/>
      {retryDrill}
      {!pendingInsight && documentChoices}
      {chosen.layer === 'source' && chosen.entered !== 'source' && chosen.data.sources?.length > 1 && <section aria-label="原件选择">{chosen.data.sources.map(row => <Row key={row.id} title={row.title} selected={chosen.data.source?.id === row.id} onOpen={() => selectDrill({ document_id: chosen.data.note?.document_id, source_id: row.id })}/>)}</section>}
      {chosen.layer === 'insight' && <>
        {!pendingInsight && grownChoices}
        {insight && <>
          {editing ? <InsightEditor key={`${chosen.data.namespace}:${scope}:${filter}:${userEpoch}:${detailProject}:${chosen.entered}:${chosen.row.id || chosen.row.document_id}`}
            projectId={insightProject} namespace={chosen.data.namespace} onNavigationGuard={registerReviewNavigation}
            insight={insight} disabled={mutating || !canEditInsight} onBusy={setEditBusy} onCancel={() => setEditing(false)}
            onAdopt={data => { drillGate.invalidate('detail'); setFailedDrill(null);
              setSelection(value => value === chosen ? { ...value, data, readonly: value.readonly || data.can_write === false || Boolean(data.insight_readonly) } : value);
              setRefresh(value => value + 1); }}
            onSaved={result => { drillGate.invalidate('detail'); setFailedDrill(null);
              setSelection(value => value === chosen ? { ...value, data: { ...value.data, insight: result, used_by: usageAfterSave(value, 'insight', result) } } : value);
              setEditing(false); setRefresh(value => value + 1); }}/>
            : <p className="library-insight-text">{insight.text}</p>}
          {!editing && insight.inspiration && <InspirationDetails projectId={detailProject} insight={insight}
            scopeKey={`${extractionKey}:${focusScope}`} onOpenDocument={evidence => openInspirationDocument(evidence, detailProject, readonlySource)}/>}
          {!pendingInsight && insightMetadata}
          {insight.hint?.target && <section className="library-grown" aria-label={pendingInsight ? '相对已有认识' : '旧认识'}>
            {pendingInsight ? <Row className="library-comparison-row" dot={<CandidateHint relation={insight.hint.relation} commentSource={insight.comment_source}/>}
              title={<span title={insight.hint.target.text}>{insight.hint.target.text}</span>} trailing={<Icon name="open" size={14}/>}
              onOpen={() => open('insight', insight.hint.target, insight.hint.target.project_id, true)}/>
              : <><Row readOnly title={insight.hint.target.text}/>
                {insight.hint.target.conditions?.length > 0 && <ul className="library-conditions">{insight.hint.target.conditions.map(value => <li key={value}>{value}</li>)}</ul>}
                <button type="button" className="library-comparison-open" aria-label="打开旧认识" onClick={() => open('insight', insight.hint.target, insight.hint.target.project_id, true)}><Icon name="open" size={14}/></button></>}
          </section>}
          {!confirming && <InsightMergeReview key={`${focusScope}:${insight.id}`} projectId={chosen.data.insight_project_id || detailProject}
            detail={chosen.data} readonly={editing || chosen.readonly} onBusy={setMergeBusy}
            onChanged={result => { setRefresh(value=>value+1); open('insight', {id:result.proposal?.state === 'approved' ? result.proposal.result_candidate_ids[0] : insight.id}, chosen.data.insight_project_id || detailProject); }}
            onOpenTarget={(row, ownerProject)=>open('insight', row, ownerProject, true)}/>}
          {pendingInsight && chosen.data.evidence?.length > 0 && <section className="library-grown library-evidence" aria-label="依据"><h3>依据</h3>
            {chosen.data.evidence.map((evidence, index) => <Row key={`${evidenceWindowKey(evidence)}:${index}`}
              title={evidence.title} sub={evidence.window.quote} onOpen={() => openEvidence(evidence)}/>)}
          </section>}
          {pendingInsight && <>{insightMetadata}{documentChoices}{grownChoices}</>}
          {!chosen.readonly && <div className="library-actions">
            {insight.state === 'pending' && <><button type="button" className="library-primary" disabled={busy} onClick={() => mutate(() => libraryApi.review(detailProject, insight, 'confirm'))}>确认</button><button type="button" disabled={busy} onClick={() => mutate(() => libraryApi.review(detailProject, insight, 'drop'))}>丢弃</button></>}
            {insight.state === 'pending' && destination && <button type="button" disabled={busy} aria-label={destination.id === 'me' ? '确认到我' : `确认到 #${destination.name}`} onClick={() => mutate(async () => { await libraryApi.confirmTo(detailProject, insight, destination.id); return { lifecycle: true }; })}>{destination.id === 'me' ? <Icon name="person" size={16}/> : `#${destination.name}`}</button>}
            {canEditInsight && <button type="button" disabled={busy} onClick={() => { drillGate.invalidate('detail'); setFailedDrill(null); setEditing(true); }}>编辑</button>}
            {insight.kind === 'candidate' && insight.state === 'forgotten' && <button type="button" disabled={busy} onClick={() => mutate(() => libraryApi.forget(detailProject, insight, false))}>捡回</button>}
            {insight.kind === 'recognition' && ['active', 'forgotten'].includes(insight.state) && <button type="button" disabled={busy} onClick={() => mutate(() => libraryApi.forget(detailProject, insight, !insightRecoverable))}>{insightRecoverable ? '恢复' : '遗忘'}</button>}
            {insight.kind === 'recognition' && <button ref={insightMoreTrigger} type="button" aria-expanded={more} onClick={()=>setMore(value=>!value)}>更多</button>}
          </div>}
          {more && <div className="library-actions">{[['split','拆分'],['merge','合并'],['versions','版本'],['erase','永久删除']].filter(([key])=>!['split','merge'].includes(key)||insight.state==='active').map(([key,name])=><button key={key} type="button" onClick={()=>{drillGate.invalidate('detail');setMaintenance(key);setMore(false);if(key==='versions')insightMoreTrigger.current?.focus();}}>{name}</button>)}
            {!readonlySource && insight.kind === 'recognition' && insight.state === 'active' && insight.conditions?.length > 0 && <button type="button" onClick={() => { setSkillExport({ scope, projectId: detailProject, scene: insight.scene, sourceId: insight.id }); setMore(false); }}>导出为 skill</button>}
          </div>}
          {maintenance && <InsightMaintenance key={`${scope}:${filter}:${userEpoch}:${detailProject}:${insight.id}:${maintenance}`} projectId={detailProject} insight={insight} insights={visible?.insight?.items || []} action={maintenance} onReload={reloadMaintenance} onDone={()=>{setSelection(null);setMore(false);setMaintenance(null);setRefresh(value=>value+1);}}/>}
          {insight.kind === 'recognition' && <InsightLinks key={`${detailProject}:${insight.id}`} projectId={detailProject} insightId={insight.id} namespace={chosen.data.namespace} insights={visible?.insight?.items || []} readonly={chosen.readonly} onOpen={(row, ownerProjectId, originMeta) => {
            if (originMeta?.namespace && originMeta.namespace !== chosen.data.namespace) { setError('来源已变化 · 重新读取'); return; }
            open('insight', row, ownerProjectId || row.project_id || detailProject, chosen.readonly || Boolean(originMeta?.readonly), originMeta);
          }} onReviewed={() => setRefresh(value => value + 1)}/>}
          <ConsolidationSuggestions key={`consolidation:${detailProject}:${insight.id}:${insight.revision}`} projectId={detailProject} insightId={insight.id} originals={insight.merged_from} readonly={chosen.readonly} onOpenDocument={(id,sourceProject)=>open('note',{document_id:id},sourceProject, chosen.readonly)} onReviewed={()=>{setSelection(null);setRefresh(value=>value+1);}}/>
          {insight.related?.length > 0 && <section className="library-grown"><h3>相关</h3>{insight.related.map(row => <button type="button" key={row.id} onClick={() => open('insight', row, detailProject, chosen.readonly)}>{row.text}</button>)}</section>}
        </>}
      </>}
      {chosen.layer === 'summary' && chosen.data.summary && <><p className="library-meta">{date(chosen.data.summary.created_at)}</p><h3 className="library-title">{chosen.data.summary.title}</h3><p className="library-summary-text">{chosen.data.summary.text}</p></>}
      {chosen.layer === 'note' && chosen.data.note && <>{readonlySource || noteArchived
        ? <MarkdownBody omitEmptyArtifacts evidence={chosen.data.note.facts.filter(fact => fact.evidence.quote === chosen.data.source?.window?.quote)} onEvidence={chosen.data.source?.window ? () => jump('source') : undefined}
          documentId={chosen.data.note.document_id} documentRevision={chosen.data.note.revision} commentSection={chosen.data.note.comment_section}>{chosen.data.note.markdown}</MarkdownBody>
        : <DocumentMarkdownEditor key={`${scope}:${detailProject}:${chosen.data.note.document_id}`} projectId={detailProject} documentId={chosen.data.note.document_id} document={chosen.data.note} disabled={busy}
          onEditingStateChange={noteEditorChanged}
          evidence={chosen.data.note.facts.filter(fact => fact.evidence.quote === chosen.data.source?.window?.quote)} onEvidence={chosen.data.source?.window ? () => jump('source') : undefined}
          readDocument={async () => (await libraryApi.drill(detailProject, 'note', chosen.data.note.document_id)).note}
          onSaved={document => { drillGate.invalidate('detail'); setFailedDrill(null); setSelection(value => value?.scope === scope && value.data === chosen.data ? { ...value, data: { ...value.data, note: { ...value.data.note, ...document, verified: true }, used_by: usageAfterSave(value, 'note', document) } } : value); setRefresh(value => value + 1); }}/>
      }
        <DocumentPlacement key={`${detailProject}:${chosen.data.note.document_id}:${chosen.data.note.revision}`} projectId={detailProject}
          projects={projects} row={chosen.data.note} readonly={Boolean(readonlySource || noteArchived)} onError={setError}
          onChanged={result => {
            if (!drillGate.isScopeCurrent(drillScope)) return;
            setPlacementNoticeScope(result.state === 'filed' && result.error ? drillScope : null);
            setSelection(null); setRefresh(value => value + 1);
          }}/>
        {!readonlySource && <div className="library-actions">{!noteArchived && <button type="button" disabled={busy || Boolean(unsavedNote)} onClick={() => startInspiration(chosen.data.note.document_id, detailProject)}>提炼灵感</button>}<button type="button" disabled={busy} onClick={()=>mutate(async()=>{
          if (noteCooled && !noteArchived) await libraryApi.restoreRecall(detailProject, chosen.data.note);
          else await (noteArchived ? restoreDocument : archiveDocument)({projectId:detailProject,documentId:chosen.data.note.document_id,expectedRevision:chosen.data.note.revision});
          return {lifecycle:true};
        })}>{noteArchived || noteCooled ? '恢复' : '遗忘'}</button></div>}
      </>}
      {chosen.layer === 'source' && original && <><p className="library-meta">{kinds[original.kind] || '原件'}</p><h3 className="library-title">{original.title}</h3>
        {!readonlySource && !chosen.data.source_readonly && <OriginalPrivacy key={`${sourceProject}:${original.id}`} projectId={sourceProject} sourceId={original.id}/>}
        <OriginalText key={`${scope}:${original.id}:${chosen.data.note?.document_id || ''}:${chosen.evidenceKey || ''}`} projectId={sourceProject} original={original} scopeKey={`${scope}:${filter}:${userEpoch}:${sourceProject}:${original.id}:${chosen.evidenceKey || ''}:${JSON.stringify(original.window)}`}/>
        <OriginalFiles key={`${scope}:${filter}:${userEpoch}:${sourceProject}:${original.id}`} projectId={sourceProject} original={original} scopeKey={`${scope}:${filter}:${userEpoch}:${sourceProject}:${original.id}`}/>
      </>}
      <CitationUsage key={`${scope}\0${filter}\0${userEpoch}\0${focusScope}\0${chosen.entered}\0${chosen.row.id || chosen.row.document_id}\0${chosen.evidenceKey || ''}`}
        entries={chosen.data.used_by} onOpen={onNavigate ? openCitationUsage : undefined}/>
      {layers.indexOf(chosen.layer) < 3 && (chosen.data[layers[layers.indexOf(chosen.layer) + 1]] || (chosen.layer === 'insight' && chosen.data.documents?.length > 1) || (chosen.layer === 'note' && chosen.data.sources?.length > 1)) && <button type="button" className="library-down" aria-label="下一层" onClick={() => jump(layers[layers.indexOf(chosen.layer) + 1])}>↓</button>}
    </FocusPanel>}
    {chosen && skillExport?.scope === scope && skillExport.sourceId === chosen.data.insight?.id && <SkillExportPanel key={`${scope}:${detailProject}:${skillExport.sourceId}`} projectId={skillExport.projectId} scene={skillExport.scene} sourceId={skillExport.sourceId} onClose={() => setSkillExport(null)}/>}
  </div>;
}
export default Library;
