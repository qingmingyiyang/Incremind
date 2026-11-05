export function resumeAuthenticatedPage() {
  const hash = location.hash || '#view=workbench&project_id=default';
  history.replaceState(null, '', '/' + location.search + hash);
  window.dispatchEvent(new Event('hashchange'));
}
