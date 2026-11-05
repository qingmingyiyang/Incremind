const displayKinds = ['insight', 'summary', 'note', 'source', 'inspiration'];
// 更新引用沿实际source_type传输，替代认识支路使用insight；展示类型单独校验。
const updateContextKinds = ['recognition', 'experience', 'document', 'original_item', 'original_source', 'insight', 'outcome_update'];
export const outcomeChangeReasons = { source_replaced: '原件已替换', edited: '内容已修改', superseded: '已有替代认识', forgotten: '已手动遗忘', expired: '已到期' };
export function readOutcomeChanges(value) {
  if (!value || typeof value.stale !== 'boolean' || typeof value.verified !== 'boolean'
    || typeof value.fresh_verified !== 'boolean' || !Array.isArray(value.changed) || !Array.isArray(value.fresh)
    || !Array.isArray(value.unverifiable) || !Number.isSafeInteger(value.ack_revision) || value.ack_revision < 0
    || !Object.hasOwn(value, 'observation')
    || !(value.fresh_count === null || Number.isSafeInteger(value.fresh_count) && value.fresh_count >= 0)
    || value.changed.some(item => !displayKinds.includes(item.kind) || typeof item.id !== 'string'
      || typeof item.project_id !== 'string' || typeof item.title !== 'string' || !Array.isArray(item.reasons)
      || item.reasons.some(reason => !Object.hasOwn(outcomeChangeReasons, reason)))
    || value.fresh.some(item => typeof item.document_id !== 'string' || typeof item.title !== 'string'))
    throw new Error('成果依据状态无效');
  return value;
}
export function readOutcomeUpdate(value, projectId, documentId) {
  if (value?.project_id !== projectId || value.continue_from !== documentId || value.intent !== 'do'
    || typeof value.text !== 'string' || !value.text.trim() || !Array.isArray(value.context_ids)
    || value.context_ids.some(item => !updateContextKinds.includes(item.kind) || typeof item.id !== 'string' || !item.id
      || typeof item.project_id !== 'string' || !item.project_id || !Number.isSafeInteger(item.revision) || item.revision <= 0)
    || value.context_ids.filter(item => item.kind === 'outcome_update').length !== 1
    || value.context_ids.some(item => item.kind === 'outcome_update' && (item.project_id !== projectId || item.revision !== 1)))
    throw new Error('成果更新记录无效');
  return value;
}
