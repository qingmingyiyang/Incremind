import { readSharedCommands, saveSharedCommand, clearSharedCommand } from '../shared-projects/sharedProjectCommands.js';

export const PROVIDER_OPERATION_EVENT = 'chriptmas-provider-operations';
const kinds = new Set(['save', 'disconnect', 'selection', 'trial']);
const notify = () => globalThis.window?.dispatchEvent(new Event(PROVIDER_OPERATION_EVENT));
function stored(action) {
  try { return action(); }
  catch { throw new Error('操作记录未核对 · 保留浏览器资料'); }
}

export function pendingProviderOperations(scope) {
  return stored(() => readSharedCommands(scope)).map(command => {
    const metadata = command.body;
    if (!kinds.has(metadata.kind) || typeof metadata.target !== 'string' || metadata.id !== command.id
        || Object.keys(metadata).some(key => !['id', 'kind', 'target', 'projectId', 'purpose'].includes(key)))
      throw new Error('操作记录无法核对 · 保留浏览器资料');
    return { ...command, kind: metadata.kind, target: metadata.target, projectId: metadata.projectId, purpose: metadata.purpose };
  });
}

export function beginProviderOperation(scope, { id, kind, target, projectId = null, purpose = null }, { newTrial = false } = {}) {
  if (!kinds.has(kind) || typeof id !== 'string' || typeof target !== 'string') throw new Error('操作身份无效');
  const pending = pendingProviderOperations(scope).find(command => (command.kind === 'trial') === (kind === 'trial'));
  if (pending && !(kind === 'trial' && newTrial)) {
    const error = new Error('已有操作待核对 · 查看待处理记录'); error.code = 'provider_operation_pending'; error.command = pending; throw error;
  }
  // 仅持久化身份和动作；请求正文、地址、密钥及试用文字均留在调用方内存。
  const metadata = { id, kind, target, projectId, purpose };
  const command = { version: 1, storageKey: scope, id, path: kind, body: metadata,
    expected: metadata, phase: 'prepared', createdAt: new Date().toISOString() };
  stored(() => saveSharedCommand(command)); notify();
  return command;
}

export function markProviderOperation(command, phase) {
  stored(() => saveSharedCommand({ ...command, phase })); notify();
}

export function finishProviderOperation(command) {
  stored(() => clearSharedCommand(command)); notify();
}
