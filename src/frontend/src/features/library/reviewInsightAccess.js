export function reviewInsightProject(detail, fallbackProject) {
  if (!detail?.insight) return null;
  if (Object.hasOwn(detail, 'insight_project_id'))
    return typeof detail.insight_project_id === 'string' && detail.insight_project_id ? detail.insight_project_id : null;
  return typeof fallbackProject === 'string' && fallbackProject ? fallbackProject : null;
}

export function writableReviewInsightProject(detail, fallbackProject) {
  const project = reviewInsightProject(detail, fallbackProject);
  if (!project || detail.can_write === false || detail.can_edit_insight === false) return null;
  const qualified = ['insight_project_id', 'can_write', 'can_edit_insight'].some(key => Object.hasOwn(detail, key));
  if (qualified) return detail.can_write === true && detail.can_edit_insight === true
    && typeof detail.namespace === 'string' && detail.namespace ? project : null;
  return detail.readonly ? null : project;
}

export function validateReviewInsight(detail, { id, project, namespace, fallbackProject } = {}) {
  const insight = detail?.insight;
  if (!insight || insight.id !== id || typeof insight.text !== 'string'
      || !Number.isSafeInteger(insight.revision) || insight.revision < 1 || !Array.isArray(insight.conditions)
      || insight.conditions.some(value => typeof value !== 'string')
      || !reviewInsightProject(detail, fallbackProject)
      || project !== undefined && reviewInsightProject(detail, fallbackProject) !== project
      || namespace !== undefined && detail.namespace !== namespace
      || Object.hasOwn(detail, 'can_write') && typeof detail.can_write !== 'boolean'
      || Object.hasOwn(detail, 'can_edit_insight') && typeof detail.can_edit_insight !== 'boolean')
    throw new Error('insight_refresh_invalid');
  return detail;
}
