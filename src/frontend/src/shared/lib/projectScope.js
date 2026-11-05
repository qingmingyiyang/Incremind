export const projectScopeKind = value => value?.scope_kind || 'personal';
export const projectScopeKey = value => `${projectScopeKind(value)}:${value.id}`;

export function projectScopeLabel(value, projects = []) {
  const shared = projectScopeKind(value) === 'shared';
  const duplicate = projects.some(other => other !== value && other.id !== value.id
    && projectScopeKind(other) === projectScopeKind(value) && other.name === value.name);
  return `${value.name}${shared ? value.membership === 'unavailable' ? ' · 已退出共享' : ' · 共享' : ''}${duplicate ? ` · ${value.id}` : ''}`;
}
