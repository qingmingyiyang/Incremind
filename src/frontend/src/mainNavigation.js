import { resolveCompanionProjectId } from "./shared/lib/projectId";

export function subscribeMainNavigation() {
  return globalThis.electronAPI?.subscribeMainNavigation?.(({ view: targetView, panel, intent, filter }) => {
    const current = new URLSearchParams(globalThis.location.hash.slice(1));
    const projectId = resolveCompanionProjectId(current.get("project_id"));
    if (targetView === "rebuild-library-overview" && filter === "pending_memory") {
      const target = new URLSearchParams({ view: 'library', filter });
      if (projectId) target.set('project_id', projectId);
      globalThis.location.hash = target.toString();
      return;
    }
    const target = new URLSearchParams({ view: 'companion', mode: panel === 'memory' || intent === 'weekly_memory_review' ? 'review' : 'chat' });
    if (intent === "weekly_memory_review") target.set("intent", intent);
    if (projectId) target.set("project_id", projectId);
    globalThis.location.hash = target.toString();
  });
}
