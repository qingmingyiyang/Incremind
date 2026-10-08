import { productFetch as fetch } from './shared/api/deviceTransport';
import { readResponseJson } from './shared/lib/responseJson';
import { lazy, useCallback, useEffect, useRef, useState } from "react";
import { Shell, Tray } from "./shared/ui";
import { PorcelainLazyRoute } from "./features/rebuild/PorcelainRouteFallback";
import { libraryBackendUrl } from "./features/rebuild/libraryOverviewTransport";
import { workspaceApi } from "./shared/api/workspaceApi";
import { workbenchApi } from "./features/workbench/workbenchApi";
import { subscribeMainNavigation } from "./mainNavigation";
import "./AppRouter.css";

const WorkbenchPage = lazy(() => import("./features/workbench/Workbench"));
const LibraryPage = lazy(() => import('./features/library/Library'));
const SettingsPage = lazy(() => import('./features/settings/SettingsPage'));
const CompanionPage = lazy(() => import('./features/companion/CompanionPage').then(module => ({ default: module.CompanionPage })));
const PROJECT_KEY = "chriptmas-v2-project-id";
const aliases = { home: 'workbench', workspace: 'workbench', recognition: 'library',
  'rebuild-library-overview': 'library', 'rebuild-project-brain': 'library',
  'rebuild-video-detail': 'library', 'rebuild-video-workflow': 'library',
  'rebuild-settings': 'settings', 'rebuild-model-selection': 'settings', 'rebuild-companion': 'companion' };

function savedProject() {
  try { return globalThis.localStorage?.getItem(PROJECT_KEY) || "default"; } catch { return "default"; }
}
function rememberProject(id) {
  try { globalThis.localStorage?.setItem(PROJECT_KEY, id); } catch { /* The current window remains usable without persistence. */ }
}
function readRoute() {
  const params = new URLSearchParams(window.location.hash.slice(1));
  const oldView = params.get('view') || 'workbench';
  let view = aliases[oldView] || oldView;
  if (oldView === 'recognition' && (params.has('task_id') || params.has('task_ref'))) view = 'workbench';
  if (!['workbench', 'library', 'settings', 'companion'].includes(view)) view = 'workbench';
  const projectId = params.get('project_id') || savedProject();
  params.set('view', view); params.set('project_id', projectId);
  const hash = `#${params.toString()}`;
  if (window.location.hash !== hash) window.history.replaceState(null, '', hash);
  return { hash, view, projectId };
}
async function getV2(path, signal) {
  const response = await fetch(libraryBackendUrl(`/api/v2/${path}`), { signal, cache: "no-store", headers: { Accept: "application/json" } });
  const value = await readResponseJson(response);
  if (!response.ok) throw new Error("进度暂不可用 · 重试");
  return value;
}
function activeView(view) { return view; }

export function AppRouter() {
  const [route, setRoute] = useState(readRoute);
  const drafts = useRef(new Map()), handoffSequence = useRef(0);
  const [handoff, setHandoff] = useState(null);
  const saveDraft = useCallback(value => { drafts.current.set(route.projectId, value); }, [route.projectId]);
  const finishHandoff = useCallback(id => { setHandoff(value => value?.id === id ? null : value); }, []);
  const [projects, setProjects] = useState([]);
  const [jobs, setJobs] = useState([]);
  const [error, setError] = useState("");
  const [refreshKey, setRefreshKey] = useState(0);
  const currentProject = useRef(route.projectId);
  currentProject.current = route.projectId;
  useEffect(() => {
    const update = () => setRoute(readRoute());
    window.addEventListener("hashchange", update);
    const unsubscribe = subscribeMainNavigation();
    update();
    return () => { window.removeEventListener("hashchange", update); unsubscribe?.(); };
  }, []);
  useEffect(() => { rememberProject(route.projectId); }, [route.projectId]);
  useEffect(() => {
    const controller = new AbortController();
    getV2("projects", controller.signal).then(value => { if (!controller.signal.aborted) setProjects(Array.isArray(value.items) ? value.items : []); }).catch(() => {});
    return () => controller.abort();
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    let timer;
    let processing = false;
    let failures = 0;
    const projectId = route.projectId;
    setJobs([]); setError("");
    async function poll() {
      try {
        const value = await getV2(`jobs?${new URLSearchParams({ project_id: projectId })}`, controller.signal);
        if (controller.signal.aborted || currentProject.current !== projectId) return;
        const rows = Array.isArray(value.items) ? value.items : [];
        processing = rows.some(job => job.state === "processing");
        failures = 0;
        setJobs(rows); setError("");
      } catch {
        if (controller.signal.aborted || currentProject.current !== projectId) return;
        failures = Math.min(failures + 1, 6);
        setError("进度暂不可用 · 重试");
      }
      if (!controller.signal.aborted) timer = window.setTimeout(poll, failures ? Math.min(1000 * 2 ** (failures - 1), 30000) : processing ? 1500 : 15000);
    }
    poll();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [route.projectId, refreshKey]);
  function navigate(view, extras = {}) {
    const { compose, ...parameters } = extras;
    if (view === 'workbench' && compose?.intent === 'remember') setHandoff({ id: ++handoffSequence.current, projectId: parameters.project_id || route.projectId, scene: compose.scene, intent: 'remember' });
    else setHandoff(null);
    const params = new URLSearchParams({ view, project_id: route.projectId, ...parameters });
    window.location.hash = params.toString();
    setRoute(readRoute());
  }
  const reloadProjects = useCallback(() => {
    getV2("projects").then(value => setProjects(Array.isArray(value.items) ? value.items : [])).catch(() => {});
  }, []);
  function projectCreated(project) {
    setProjects(current => current.some(row => row.id === project.id) ? current : [...current, project]);
  }
  function switchProject(id) {
    setHandoff(null);
    const params = new URLSearchParams(route.hash.slice(1));
    params.set("project_id", id);
    for (const key of ["item_id", "task_ref", "task_id", "turn_id", "thread_id", "document_id", "source_id", "recognition_id", "candidate_id"]) params.delete(key);
    rememberProject(id);
    window.location.hash = params.toString();
    setRoute(readRoute());
  }
  function openJob(job) {
    const target = job.target;
    if (!target?.id) return;
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
  const projectList = projects.some(project => project.id === route.projectId) ? projects : [{ id: route.projectId, name: ({ default: '日常', inbox: '收件箱', me: '我' })[route.projectId] || route.projectId }, ...projects];
  const desktopTitlebar = globalThis.electronAPI?.shell === "electron" && globalThis.electronAPI?.entry === "workspace";
  const params = new URLSearchParams(route.hash.slice(1));
  return <>
    {desktopTitlebar && <header className="desktop-titlebar" aria-label="窗口标题栏"><span>Chriptmas OS</span></header>}
    <Shell active={activeView(route.view)} project={route.projectId} projects={projectList} onProjectChange={switchProject} onNavigate={navigate} onCompanion={() => navigate("companion")} bearState={jobs.some(job => job.state === "failed" || job.state === "pending") ? "attention" : jobs.some(job => job.state === "processing") ? "working" : "ready"} tray={<><Tray jobs={jobs} onOpen={openJob} onRetry={retryJob}/>{error && <div className="app-router-error"><span>{error}</span><button type="button" aria-label={error} onClick={() => setRefreshKey(value => value + 1)}>重试</button></div>}</>}>
      <div className="app-router-page" data-page-view={route.view}>
        {view === "workbench" ? <PorcelainLazyRoute><WorkbenchPage key={route.projectId} draft={drafts.current.get(route.projectId)} onDraft={saveDraft} handoff={handoff?.projectId === route.projectId ? handoff : null} onHandoff={finishHandoff} projectId={route.projectId} projects={projects} threadId={params.get("thread_id") || undefined} turnId={params.get("turn_id") || undefined} onNavigate={navigate} onProjectCreated={projectCreated} onProjectsStale={reloadProjects}/></PorcelainLazyRoute>
          : view === 'library' ? <PorcelainLazyRoute><LibraryPage key={route.hash} projectId={route.projectId} projects={projects} initialLayer={params.get('layer') || undefined} documentId={params.get('document_id') || undefined} onNavigate={navigate} onProjectCreated={projectCreated}/></PorcelainLazyRoute>
          : view === 'settings' ? <PorcelainLazyRoute><SettingsPage key={route.hash} projectId={route.projectId} initialSection={params.get('section') || 'model'} expandProject={params.get('expand_project') || undefined}/></PorcelainLazyRoute>
          : <PorcelainLazyRoute><CompanionPage key={route.hash} projectId={route.projectId} initialMode={params.get('mode') || (params.get('intent') === 'weekly_memory_review' || params.get('panel') === 'memory' ? 'review' : 'chat')}/></PorcelainLazyRoute>}

      </div>
    </Shell>
  </>;
}
