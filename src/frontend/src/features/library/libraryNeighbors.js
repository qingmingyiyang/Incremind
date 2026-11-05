export function listedInsightNeighbors(results, ids, { namespace } = {}) {
  const rows = results.flatMap(({ project, result }) =>
    (result.items || []).flatMap(item => {
      if (item.project_id !== undefined && item.project_id !== project) return [];
      if (item.namespace !== undefined && result.namespace !== undefined && item.namespace !== result.namespace) return [];
      const actualNamespace = item.namespace ?? result.namespace;
      if (namespace !== undefined && actualNamespace !== namespace) return [];
      return [{ item, project, originMeta: typeof actualNamespace === 'string' && actualNamespace
        ? { namespace: actualNamespace, readonly: result.readonly === true || item.readonly === true } : undefined }];
    }));
  return Object.fromEntries(ids.map(id => {
    const matches = rows.filter(row => row.item.id === id), first = matches[0];
    const ambiguous = matches.some(row => row.project !== first.project || row.originMeta?.namespace !== first.originMeta?.namespace);
    return [id, ambiguous ? undefined : first];
  }));
}

export function localInsightNeighbors(items, ids, { project, namespace, listProject, listNamespace } = {}) {
  const allowed = [...new Set([project, 'me'])];
  if (listProject && allowed.includes(listProject))
    return listedInsightNeighbors([{ project: listProject, result: { items, namespace: listNamespace } }], ids, { namespace });
  // 无明确列表范围时，仅复用与当前详情来源一致的完整单条归属。
  if (!namespace) return {};
  return listedInsightNeighbors(allowed.map(owner => ({ project: owner, result: { namespace,
    items: items.filter(item => item.project_id === owner && item.namespace === namespace) } })), ids, { namespace });
}
