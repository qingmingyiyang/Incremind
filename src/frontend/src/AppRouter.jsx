import { productFetch as fetch, deviceIdentity, isDeviceIdentity, userStorageKey } from './shared/api/deviceTransport';
import { readResponseJson } from './shared/lib/responseJson';
import { useRequestScope } from './shared/lib/useRequestScope';
import { useWorkbenchDraft } from './shared/lib/workbenchDraft';
import { lazy, useCallback, useEffect, useRef, useState } from "react";
import { Shell, Tray } from "./shared/ui";
import { PorcelainLazyRoute } from "./features/rebuild/PorcelainRouteFallback";
import { libraryBackendUrl } from "./features/rebuild/libraryOverviewTransport";
import { workspaceApi } from "./shared/api/workspaceApi";
import { workbenchApi } from "./features/workbench/workbenchApi";
import { subscribeMainNavigation } from "./mainNavigation";
import "./AppRouter.css";
import { useReminderNotices } from './features/companion/useReminderNotices';
import { HumanAuthorization } from './features/externalAgent/HumanAuthorization';
import { useSharedScopes } from './features/shared-projects/useSharedScopes';
import { projectScopeKind, projectScopeKey } from './shared/lib/projectScope';

const WorkbenchPage = lazy(() => import("./features/workbench/Workbench"));
const LibraryPage = lazy(() => import('./features/library/Library'));
const SettingsPage = lazy(() => import('./features/settings/SettingsPage'));
const CompanionPage = lazy(() => import('./features/companion/CompanionPage').then(module => ({ default: module.CompanionPage })));
const PROJECT_KEY = "chriptmas-v2-project-id";
const HISTORY_KEY = 'chriptmas-v2-history';
function historyPoint() {
  const entry = window.navigation?.currentEntry;
  if (entry && Number.isSafeInteger(entry.index) && entry.index >= 0)
    return { session: 'navigation', index: entry.index, key: entry.key };
  const point = window.history.state?.[HISTORY_KEY];
  return point && typeof point.session === 'string' && Number.isSafeInteger(point.index) ? point : null;
}
function markHistory(point) {
  window.history.replaceState({ ...window.history.state, [HISTORY_KEY]: point }, '', window.location.href);
}
const aliases = { home: 'workbench', workspace: 'workbench', recognition: 'library',
  'rebuild-library-overview': 'library', 'rebuild-project-brain': 'library',
  'rebuild-video-detail': 'library', 'rebuild-video-workflow': 'library',
  'rebuild-settings': 'settings', 'rebuild-model-selection': 'settings', 'rebuild-companion': 'companion' };

function savedProject() {
  try { return globalThis.localStorage?.getItem(PROJECT_KEY) || "default"; } catch { return "default"; }
}
function savedScope() {
  try {
    const value = JSON.parse(globalThis.localStorage?.getItem(userStorageKey(`${PROJECT_KEY}:scope`)) || 'null');
    if (value && typeof value.id === 'string' && ['personal', 'shared'].includes(value.kind)) return value;
  } catch { /* 保留当前页面，存储失败不改变来源身份。 */ }
  return { id: userStorageKey(PROJECT_KEY) === PROJECT_KEY ? savedProject() : 'default', kind: 'personal' };
}
function rememberProject(id, kind = 'personal') {
  if (!['personal', 'shared'].includes(kind)) return;
  try {
    globalThis.localStorage?.setItem(userStorageKey(`${PROJECT_KEY}:scope`), JSON.stringify({ id, kind }));
    if (kind === 'personal') globalThis.localStorage?.setItem(PROJECT_KEY, id);
  } catch { /* 存储不可用时，当前路由继续保留完整来源身份。 */ }
}
function readRoute() {
  const params = new URLSearchParams(window.location.hash.slice(1));
  const oldView = params.get('view') || 'workbench';
  let view = aliases[oldView] || oldView;
  if (oldView === 'recognition' && (params.has('task_id') || params.has('task_ref'))) view = 'workbench';
  if (!['workbench', 'library', 'settings', 'companion'].includes(view)) view = 'workbench';
  const saved = savedScope(), projectId = params.get('project_id') || saved.id;
  const requestedKind = params.get('scope_kind') || (params.has('project_id') ? 'personal' : saved.kind);
  const scopeKind = ['personal', 'shared'].includes(requestedKind) ? requestedKind : 'unavailable';
  if (scopeKind === 'shared') params.set('scope_kind', 'shared');
  else if (scopeKind === 'personal') params.delete('scope_kind');
  params.set('view', view); params.set('project_id', projectId);
  const hash = `#${params.toString()}`;
  if (window.location.hash !== hash) window.history.replaceState(window.history.state, '', hash);
  return { hash, view, projectId, scopeKind };
}
async function getV2(path, signal) {
  const response = await fetch(libraryBackendUrl(`/api/v2/${path}`), { signal, cache: "no-store", headers: { Accept: "application/json" } });
  const value = await readResponseJson(response);
  if (!response.ok) throw new Error("进度暂不可用 · 重试");
  return value;
}
function activeView(view) { return view; }
const validInspirationJob = job => job.kind !== 'inspiration' || typeof job.id === 'string' && Boolean(job.id)
  && typeof job.attention === 'boolean' && Number.isSafeInteger(job.pending_count) && job.pending_count >= 0
  && Number.isSafeInteger(job.unread_count) && job.unread_count >= 0
  && ['processing', 'pending', 'failed', 'done'].includes(job.state) && job.target?.type === 'inspiration'
  && job.target.id === job.id && typeof job.target.project_id === 'string' && Boolean(job.target.project_id);

export function AppRouter() {
  const routeIdentity = deviceIdentity();
  const [route, setRoute] = useState(readRoute);
  const historyAtRoute = useRef(null), historyTraversal = useRef(null);
  const [historyError, setHistoryError] = useState('');
  if (!historyAtRoute.current) {
    historyAtRoute.current = historyPoint() || { session: crypto.randomUUID(), index: 0 };
    markHistory(historyAtRoute.current);
  }
  const handoffSequence = useRef(0);
  const [handoff, setHandoff] = useState(null);
  const finishHandoff = useCallback(id => { setHandoff(value => value?.id === id ? null : value); }, []);
  const finishInputProposal = useCallback(() => {
    const params = new URLSearchParams(window.location.hash.slice(1));
    params.delete('compose_text'); params.delete('compose_scene');
    window.history.replaceState(window.history.state, '', `#${params.toString()}`); setRoute(readRoute());
  }, []);
  const [projects, setProjects] = useState([]);
  const [jobs, setJobs] = useState([]);
  const [error, setError] = useState("");
  const [failedJobOpen, setFailedJobOpen] = useState(null), [userEpoch, setUserEpoch] = useState(0);
  const shared = useSharedScopes(userEpoch, `${route.scopeKind}:${route.projectId}:${route.view}`);
  const reminderNotices = useReminderNotices(projects, userEpoch);
  const { draft, saveDraft, draftKey } = useWorkbenchDraft(route.projectId, userEpoch, route.scopeKind);
  const [originalOpenSequence, setOriginalOpenSequence] = useState(0);
  const jobOpenGate = useRequestScope(`${route.hash}\0${userEpoch}`), jobOpenScope = jobOpenGate.scope;
  const jobOpenError = jobOpenGate.isCurrent(failedJobOpen?.request) ? failedJobOpen : null;
  const [refreshKey, setRefreshKey] = useState(0);
  const currentProject = useRef(route.projectId);
  currentProject.current = route.projectId;
  const currentView = useRef(route.view), libraryNavigationBlocked = useRef(null);
  const routeAtNavigation = useRef(route); routeAtNavigation.current = route;
  currentView.current = route.view;
  const requestLibraryNavigation = useCallback((action, options) => {
    if (historyTraversal.current && ['restoring', 'accepted'].includes(historyTraversal.current.phase)) return false;
    const report = libraryNavigationBlocked.current;
    if (currentView.current === 'library' && report?.projectId === currentProject.current
        && isDeviceIdentity(report.identity) && typeof report.requestNavigation === 'function')
      return report.requestNavigation(action, options);
    action(); return true;
  }, []);
  const reportLibraryNavigation = useCallback(report => {
    if (report.disposed) {
      if (libraryNavigationBlocked.current?.scope === report.scope) libraryNavigationBlocked.current = null;
      return;
    }
    if (isDeviceIdentity(report.identity) && currentView.current === 'library' && currentProject.current === report.projectId)
      libraryNavigationBlocked.current = report;
  }, []);
  useEffect(() => {
    const changed = () => {
      jobOpenGate.invalidate('open'); libraryNavigationBlocked.current = null;
      // 身份改变只撤销旧用户的目标，正在进行的指针恢复仍须完成。
      if (historyTraversal.current?.phase === 'waiting') historyTraversal.current = null;
      else if (historyTraversal.current) historyTraversal.current.cancelled = true;
      setFailedJobOpen(null); setJobs([]); setProjects([]); setError(''); setUserEpoch(value => value + 1);
    };
    window.addEventListener('chriptmas-user-space', changed);
    window.addEventListener('chriptmas-device-required', changed);
    return () => {
      window.removeEventListener('chriptmas-user-space', changed);
      window.removeEventListener('chriptmas-device-required', changed);
    };
  }, []);
  useEffect(() => {
    let disposed = false;
    const adopt = next => {
      historyTraversal.current = null; historyAtRoute.current = historyPoint();
      setHistoryError(''); routeAtNavigation.current = next; setRoute(next);
    };
    const traverse = (point, attempt) => {
      const current = historyPoint();
      const requestKey = `${current?.session}:${current?.key ?? current?.index}:${point.key ?? point.index}`;
      if (attempt.lastTraversal === requestKey) return;
      attempt.lastTraversal = requestKey;
      if (point.key && window.navigation?.traverseTo) {
        const result = window.navigation.traverseTo(point.key);
        // 目标可能被新的历史条目截掉，失败后解除导航占用并保留编辑器。
        Promise.all([result.committed, result.finished]).catch(() => {
          if (disposed || historyTraversal.current !== attempt) return;
          historyTraversal.current = null;
          historyAtRoute.current = historyPoint() || historyAtRoute.current;
          setHistoryError('历史目标已失效，当前编辑仍保留，请使用应用内导航重新选择');
        });
      } else {
        if (current?.session === point.session) window.history.go(point.index - current.index);
      }
    };
    const ask = attempt => {
      attempt.phase = 'waiting';
      if (attempt.cancelled || !isDeviceIdentity(attempt.identity)) { historyTraversal.current = null; return; }
      requestLibraryNavigation(() => {
        if (historyTraversal.current !== attempt || !isDeviceIdentity(attempt.identity)) return;
        attempt.phase = 'accepted'; traverse(attempt.target, attempt);
      }, { onCancel: () => { if (historyTraversal.current === attempt) historyTraversal.current = null; } });
    };
    const update = () => {
      const next = readRoute(), previous = routeAtNavigation.current, current = historyPoint();
      const attempt = historyTraversal.current;
      if (attempt && current?.session === attempt.origin.session) {
        if (attempt.phase === 'accepted' && current.index === attempt.target.index) {
          if (attempt.cancelled || !isDeviceIdentity(attempt.identity)) {
            attempt.phase = 'restoring'; traverse(attempt.origin, attempt);
          } else adopt(next);
          return;
        }
        if (attempt.phase === 'restoring') {
          if (current.index === attempt.origin.index) ask(attempt);
          else traverse(attempt.origin, attempt);
          return;
        }
        if (attempt.phase === 'waiting') {
          if (current.index !== attempt.origin.index) {
            attempt.phase = 'restoring'; traverse(attempt.origin, attempt);
          }
          return;
        }
        if (attempt.phase === 'accepted') { traverse(attempt.target, attempt); return; }
      }
      if (next.hash === previous.hash) { historyAtRoute.current = current || historyAtRoute.current; return; }
      const origin = historyAtRoute.current;
      if (!current || current.session !== origin.session) {
        // 旧浏览器无法定位挂载前的无编号条目，保留编辑器并明确说明能力边界。
        setHistoryError('浏览器无法恢复这条历史位置，当前草稿仍保留，请先保存或使用应用内导航');
        requestLibraryNavigation(() => adopt(next));
        return;
      }
      const pending = { origin, target: current, identity: deviceIdentity(), phase: 'restoring' };
      historyTraversal.current = pending;
      traverse(origin, pending);
    };
    window.addEventListener('popstate', update);
    window.addEventListener("hashchange", update);
    const unsubscribe = subscribeMainNavigation();
    update();
    return () => { disposed = true; window.removeEventListener('popstate', update); window.removeEventListener("hashchange", update); unsubscribe?.(); };
  }, []);
  useEffect(() => { rememberProject(route.projectId, route.scopeKind); }, [route.projectId, route.scopeKind]);
  useEffect(() => {
    const controller = new AbortController(), identity = deviceIdentity();
    getV2("projects", controller.signal).then(value => { if (!controller.signal.aborted && isDeviceIdentity(identity)) setProjects(Array.isArray(value.items) ? value.items : []); }).catch(() => {});
    return () => controller.abort();
  }, [userEpoch]);
  useEffect(() => {
    const controller = new AbortController(), identity = deviceIdentity();
    let timer;
    let processing = false;
    let failures = 0;
    const projectId = route.projectId;
    setJobs([]); setError("");
    if (route.scopeKind !== 'personal') return;
    async function poll() {
      try {
        const value = await getV2(`jobs?${new URLSearchParams({ project_id: projectId })}`, controller.signal);
        if (controller.signal.aborted || currentProject.current !== projectId || !isDeviceIdentity(identity)) return;
        const rows = Array.isArray(value.items) ? value.items : [];
        if (!rows.every(validInspirationJob)) throw new Error('invalid_inspiration_job');
        processing = rows.some(job => job.state === "processing");
        failures = 0;
        setJobs(rows); setError("");
      } catch {
        if (controller.signal.aborted || currentProject.current !== projectId || !isDeviceIdentity(identity)) return;
        failures = Math.min(failures + 1, 6);
        setError("进度暂不可用 · 重试");
      }
      if (!controller.signal.aborted) timer = window.setTimeout(poll, failures ? Math.min(1000 * 2 ** (failures - 1), 30000) : processing ? 1500 : 15000);
    }
    poll();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [route.projectId, route.scopeKind, refreshKey, userEpoch]);
  useEffect(() => {
    const changed = () => setRefreshKey(value => value + 1);
    window.addEventListener('chriptmas-inspirations-read', changed);
    return () => window.removeEventListener('chriptmas-inspirations-read', changed);
  }, []);
  function navigate(view, extras = {}, reopenLibrary = false) {
    if (!isDeviceIdentity(routeIdentity)) return;
    requestLibraryNavigation(() => {
      if (!isDeviceIdentity(routeIdentity)) return;
      jobOpenGate.invalidate('open'); setFailedJobOpen(null);
      if (reopenLibrary) setOriginalOpenSequence(value => value + 1);
      const { compose, ...parameters } = extras;
      const explicitProject = parameters.project_id !== undefined;
      let kind = parameters.scope_kind || (explicitProject ? 'personal' : route.scopeKind);
      let project = parameters.project_id || route.projectId;
      if (!explicitProject && ['settings', 'companion'].includes(view) && kind !== 'personal') {
        project = projects.some(row => row.id === savedProject()) ? savedProject() : projects[0]?.id || 'default';
        kind = 'personal';
      }
      if (view === 'workbench' && compose?.intent === 'remember') setHandoff({ id: ++handoffSequence.current, projectId: parameters.project_id || route.projectId, scene: compose.scene, intent: 'remember' });
      else setHandoff(null);
      const params = new URLSearchParams({ view, ...parameters, project_id: project });
      if (kind !== 'personal') params.set('scope_kind', kind); else params.delete('scope_kind');
      commitNavigation(params);
    });
  }
  function commitNavigation(params) {
    historyTraversal.current = null;
    const hash = `#${params.toString()}`;
    if (window.location.hash !== hash) {
      const current = historyPoint() || historyAtRoute.current;
      window.history.pushState({ ...window.history.state, [HISTORY_KEY]: { session: current.session, index: current.index + 1 } }, '', hash);
    }
    historyAtRoute.current = historyPoint(); setHistoryError('');
    const next = readRoute(); routeAtNavigation.current = next; setRoute(next);
  }
  const reloadProjects = useCallback(() => {
    const identity = deviceIdentity();
    getV2("projects").then(value => { if (isDeviceIdentity(identity)) setProjects(Array.isArray(value.items) ? value.items : []); }).catch(() => {});
  }, []);
  function projectCreated(project) {
    setProjects(current => current.some(row => row.id === project.id) ? current.map(row => row.id === project.id ? project : row) : [...current, project]);
  }
  function switchProject(id, selected) {
    if (!isDeviceIdentity(routeIdentity)) return;
    const kind = projectScopeKind(selected), rows = kind === 'shared' ? shared.items : projects;
    if (!rows.some(row => row.id === id)) return;
    requestLibraryNavigation(() => {
      if (!isDeviceIdentity(routeIdentity)) return;
      jobOpenGate.invalidate('open'); setFailedJobOpen(null);
      setHandoff(null);
      const params = new URLSearchParams(route.hash.slice(1));
      params.set("project_id", id);
      if (kind === 'shared') { params.set('scope_kind', 'shared'); params.set('view', 'workbench'); }
      else params.delete('scope_kind');
      for (const key of ["item_id", "task_ref", "task_id", "turn_id", "thread_id", "document_id", "source_id", "insight_id", "recognition_id", "candidate_id", "inspiration_job_id", "compose_text", "compose_scene"]) params.delete(key);
      rememberProject(id, kind);
      commitNavigation(params);
    });
  }
  async function openJob(job) {
    const target = job.target;
    if (!target?.id) return;
    const request = jobOpenGate.issue('open', jobOpenScope);
    if (!request) return;
    setFailedJobOpen(null);
    if (target.type === 'inspiration') {
      const blocked = libraryNavigationBlocked.current;
      if (currentView.current === 'library' && blocked?.projectId === currentProject.current
        && blocked.blocked && (!blocked.requestNavigation || blocked.nonReviewBlocked) && isDeviceIdentity(blocked.identity)) {
        setFailedJobOpen({ request, job, blocked: true }); return;
      }
      if (typeof target.project_id !== 'string' || !target.project_id) return;
      navigate('library', { project_id: target.project_id, layer: 'insight', filter: 'pending_memory', inspiration_job_id: target.id }, true);
      return;
    }
    if (target.type === 'item' && !target.document_id) {
      try {
        const sources = await getV2(`library/sources?${new URLSearchParams({ project_id: route.projectId })}`);
        if (!jobOpenGate.isCurrent(request)) return;
        if (sources.items.some(source => source.id === target.id)) {
          navigate('library', { layer: 'source', source_id: target.id }, true);
          return;
        }
      } catch {
        if (jobOpenGate.isCurrent(request)) setFailedJobOpen({ request, job });
        return;
      }
    }
    if (target.type === "turn") {
      navigate("workbench", { turn_id: target.turn_id || target.id, ...(target.thread_id ? { thread_id: target.thread_id } : {}) });
    } else if (target.type === "document") {
      navigate("library", { layer: "note", document_id: target.id });
    } else if (["item", "task"].includes(target.type)) {
      navigate("library", { layer: "note", ...(target.document_id ? { document_id: target.document_id } : {}) });
    }
  }
  async function retryJob(job) {
    const projectId = route.projectId;
    if (job.target?.type === 'backup') {
      try {
        const response = await fetch(libraryBackendUrl(`/api/v2/jobs/${encodeURIComponent(job.target.id)}/retry`), { method: 'POST' });
        await readResponseJson(response);
        if (!response.ok) throw new Error('backup_failed');
        if (currentProject.current === projectId) setRefreshKey(value => value + 1);
      } catch { if (currentProject.current === projectId) setError('重试未完成 · 刷新'); }
      return;
    }
    if (job.target?.type === "turn") {
      try {
        const turn = await workbenchApi.retry(projectId, job.target.id);
        if (currentProject.current !== projectId) return;
        navigate("workbench", { turn_id: turn.id, thread_id: turn.thread_id });
        setRefreshKey(value => value + 1);
      } catch { if (currentProject.current === projectId) setError("重试未完成 · 刷新"); }
      return;
    }
    if (job.target?.type !== "item") { openJob(job); return; }
    try {
      const projection = await workspaceApi.list(projectId);
      if (currentProject.current !== projectId) return;
      const rows = Array.isArray(projection) ? projection : projection.items || [];
      const item = rows.find(row => row.id === job.target.id);
      if (item?.status === "failed") await workspaceApi.retry(projectId, job.target.id);
      if (currentProject.current !== projectId) return;
      openJob(job);
      setRefreshKey(value => value + 1);
    } catch {
      if (currentProject.current === projectId) setError("重试未完成 · 刷新");
    }
  }
  const view = route.view;
  const projectList = [...projects.map(row => ({ ...row, scope_kind: 'personal' })), ...shared.items];
  if (!projectList.some(project => projectScopeKey(project) === `${route.scopeKind}:${route.projectId}`))
    projectList.unshift({ id: route.projectId, scope_kind: route.scopeKind, name: ({ default: '日常', inbox: '收件箱', me: '我' })[route.projectId] || route.projectId });
  const selectedShared = route.scopeKind === 'shared' ? shared.items.find(row => row.id === route.projectId) : undefined;
  const desktopTitlebar = globalThis.electronAPI?.shell === "electron" && globalThis.electronAPI?.entry === "workspace";
  const params = new URLSearchParams(route.hash.slice(1));
  return <>
    {desktopTitlebar && <header className="desktop-titlebar" aria-label="窗口标题栏"><span>Chriptmas OS</span></header>}
    <Shell active={activeView(route.view)} project={route.projectId} projectScopeKind={route.scopeKind} projects={projectList} onProjectChange={switchProject} onNavigate={navigate} onCompanion={() => navigate("companion", reminderNotices.items.length ? {project_id:reminderNotices.items[0].project_id,tab:'schedule'} : {})} reminderNotice={reminderNotices.items.length > 0} bearState={jobs.some(job => job.kind === 'inspiration' ? job.attention === true : job.state === "failed" || job.state === "pending") ? "attention" : jobs.some(job => job.state === "processing") ? "working" : "ready"} tray={<><Tray jobs={jobs} onOpen={openJob} onRetry={retryJob}/>{(jobOpenError || error) && <div className="app-router-error"><span>{jobOpenError ? jobOpenError.blocked ? '请先保存当前内容' : '进度暂不可用 · 重试' : error}</span><button type="button" aria-label={jobOpenError ? jobOpenError.blocked ? '保存后重新打开灵感' : '进度暂不可用 · 重试' : error} onClick={() => jobOpenError ? openJob(jobOpenError.job) : setRefreshKey(value => value + 1)}>重试</button></div>}</>}>
      <div className="app-router-page" data-page-view={route.view}>
        {historyError && <p className="app-router-error" role="alert">{historyError}</p>}
        {params.has('native-mcp-consent') || params.has('native-mcp-review') ? <HumanAuthorization routeHash={route.hash} onClose={() => {
          const next = new URLSearchParams(route.hash.slice(1));
          ['native-mcp-consent', 'native-mcp-review', 'native-mcp-target'].forEach(key => next.delete(key));
          window.location.hash = next.toString();
        }}/> : route.scopeKind !== 'personal' && !(route.scopeKind === 'shared' && ['workbench', 'library'].includes(view)) ? <p role="status">该范围的此入口尚未接通 · <button type="button" onClick={() => navigate('workbench')}>查看本人对话</button></p>
          : view === "workbench" ? <PorcelainLazyRoute><WorkbenchPage key={draftKey} draft={draft} onDraft={saveDraft} projectScopeKind={route.scopeKind} projectScope={selectedShared} scopeLoading={shared.loading} scopeError={shared.error} onScopesRetry={shared.reload} serverConversationShares={shared.server === true} handoff={handoff?.projectId === route.projectId ? handoff : null} onHandoff={finishHandoff} inputProposal={params.get('compose_text') ? { id: route.hash, text: params.get('compose_text'), scene: params.get('compose_scene') || null } : null} onInputProposalHandled={finishInputProposal} projectId={route.projectId} projects={projects} threadId={params.get("thread_id") || undefined} turnId={params.get("turn_id") || undefined} onNavigate={navigate} onProjectCreated={projectCreated} onProjectsStale={reloadProjects}/></PorcelainLazyRoute>
          : view === 'library' ? <PorcelainLazyRoute><LibraryPage key={`${route.hash}\0${originalOpenSequence}\0${userEpoch}`} projectScopeKind={route.scopeKind} projectScope={selectedShared} scopeLoading={shared.loading} scopeError={shared.error} onScopesRetry={shared.reload} serverConversationShares={shared.server === true} projectId={route.projectId} projects={projects} initialLayer={params.get('layer') || undefined} initialFilter={params.get('filter') === 'pending_memory' ? 'pending' : undefined} documentId={params.get('document_id') || undefined} sourceId={params.get('source_id') || undefined} insightId={params.get('insight_id') || undefined} inspirationJobId={params.get('inspiration_job_id') || undefined} onNavigationBlocked={reportLibraryNavigation} onNavigate={navigate} onProjectCreated={projectCreated}/></PorcelainLazyRoute>
          : view === 'settings' ? <PorcelainLazyRoute><SettingsPage key={`${route.hash}\0${userEpoch}`} projectId={route.projectId} initialSection={params.get('section') || 'model'} initialPurpose={params.get('purpose') || undefined} expandProject={params.get('expand_project') || undefined}/></PorcelainLazyRoute>
          : <PorcelainLazyRoute><CompanionPage key={route.hash} projectId={route.projectId} notices={reminderNotices} onNavigate={navigate} initialReminderId={params.get('reminder_id') || ''} initialTab={params.get('tab') || undefined} initialMode={params.get('mode') || (params.get('intent') === 'weekly_memory_review' || params.get('panel') === 'memory' ? 'review' : 'chat')}/></PorcelainLazyRoute>}

      </div>
    </Shell>
  </>;
}
