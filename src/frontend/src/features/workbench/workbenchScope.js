import { scopedText } from '../../shared/lib/scopeTag';
import { threadHistoryQuery } from '../../shared/lib/threadHistory';

export function sharedAskRequest(projectId, text, files, intent) {
  threadHistoryQuery(projectId, 'shared');
  if (!Array.isArray(files) || files.length || intent && intent !== 'ask') {
    const error = new Error('共享项目当前可提问'); error.code = 'shared_intent_not_available'; throw error;
  }
  if (scopedText(text).tag) {
    const error = new Error('请从顶部选择共享项目'); error.code = 'shared_scope_tag_unsupported'; throw error;
  }
  return { project_id: projectId, scope_kind: 'shared', text, intent: 'ask', executor: 'builtin' };
}

export function sharedTaskTextTooLong(text) {
  if (text.length <= 60_000) return false;
  let length = 0;
  for (const _ of text) {
    length += 1;
    if (length > 60_000) return true;
  }
  return false;
}

export function sharedTaskRequest(projectId, text, files) {
  threadHistoryQuery(projectId, 'shared');
  if (!Array.isArray(files) || files.length || typeof text !== 'string' || !text.trim()) throw new Error('共享干活需要文字');
  if (sharedTaskTextTooLong(text)) throw new Error('共享干活最多 60,000 个字符');
  if (scopedText(text).tag) throw new Error('从顶部选择共享项目');
  return { project_id: projectId, scope_kind: 'shared', intent: 'do', executor: 'builtin', text };
}

export function sharedTaskRedoRevision(source) {
  if (source?.source_kind !== 'execution' || !Number.isSafeInteger(source.revision) || source.revision < 1)
    throw new Error('原任务执行未核对 · 重读');
  return source.revision;
}

export function sharedTaskOutcome(value, projectId, turnId, documentId) {
  const document = value?.document;
  if (value?.scope_kind !== 'shared' || value.turn_id !== turnId || !document || document.id !== documentId
      || document.project_id !== projectId || !Number.isSafeInteger(document.revision) || document.revision < 1
      || typeof document.title !== 'string' || typeof document.markdown !== 'string')
    throw new Error('原成果身份未核对');
  return document;
}

export function sharedInspirationRequest(projectId, text, files) {
  threadHistoryQuery(projectId, 'shared');
  if (!Array.isArray(files) || files.length || typeof text !== 'string' || !text.trim())
    throw new Error('共享灵感需要文字');
  if (scopedText(text).tag) throw new Error('从顶部选择共享项目');
  return { text };
}

export function sharedRememberRequest(projectId, text, files, threadId) {
  threadHistoryQuery(projectId, 'shared');
  if (!Array.isArray(files) || files.length || typeof text !== 'string' || !text.trim()) throw new Error('共享记住需要文字');
  if (scopedText(text).tag) throw new Error('从顶部选择共享项目');
  return { project_id: projectId, scope_kind: 'shared', intent: 'remember', text,
    ...(threadId ? { thread_id: threadId } : {}) };
}
