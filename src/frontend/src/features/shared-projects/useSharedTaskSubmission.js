import { useEffect, useState } from 'react';
import { deviceIdentity, isDeviceIdentity, readDeviceCredential, userStorageKey } from '../../shared/api/deviceTransport';
import { readStoredJson, removeStoredItem, writeStoredJson } from '../../shared/lib/browserStorage';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { readScopedTurn } from '../../shared/lib/threadHistory';
import { clearSharedCommand, readSharedCommand, saveSharedCommand } from './sharedProjectCommands';

const identity = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/.test(value);
const invalid = () => { throw new Error('原任务请求未核对'); };
const endpoint = '/api/v2/workbench/turns';
const acceptedKey = command => `${command.storageKey}:accepted:${command.id}`;

function taskBinding(command, threadId, turnId) {
  if (!identity(threadId) || !identity(turnId) || command.body.thread_id && threadId !== command.body.thread_id) invalid();
  return { threadId, turnId };
}

export function sharedTaskAcceptance(command, result) {
  const turn = readScopedTurn(result?.turn, 'shared'), threadId = result.thread_id;
  if (turn.intent !== 'do' || turn.thread_id !== undefined && turn.thread_id !== threadId
      || turn.project_id !== undefined && turn.project_id !== command.projectId) invalid();
  return taskBinding(command, threadId, turn.id);
}

export function sharedTaskCommandResult(command, value) {
  if (value?.command_id !== command.id || value.project_id !== command.projectId || value.scope_kind !== 'shared'
      || !['accepted', 'failed', 'preparing', 'unknown'].includes(value.state)) invalid();
  if (value.state === 'accepted') {
    return { state: value.state, accepted: taskBinding(command, value.thread_id, value.turn_id) };
  }
  if (value.state === 'failed' && (typeof value.error !== 'string' || !value.error)) invalid();
  return { state: value.state, error: value.error };
}

export const sharedTaskNotReceived = error => error?.status === 404 && error.code === 'shared_task_not_received';

function readEntry(key, projectId) {
  const command = readSharedCommand(key);
  if (!command) return null;
  const { body } = command;
  if (!identity(command.id) || command.projectId !== projectId || command.path !== endpoint
      || command.expected.kind !== 'shared-do' || body.project_id !== projectId || body.scope_kind !== 'shared'
      || body.intent !== 'do' || body.executor !== 'builtin' || typeof body.text !== 'string' || !body.text.trim()
      || body.thread_id !== undefined && !identity(body.thread_id)
      || Object.keys(body).some(field => !['project_id', 'scope_kind', 'intent', 'executor', 'text', 'thread_id'].includes(field))) invalid();
  const stored = readStoredJson(acceptedKey(command));
  if (stored.unavailable || stored.invalid) invalid();
  const accepted = stored.value;
  if (accepted && (accepted.commandId !== command.id || !identity(accepted.threadId) || !identity(accepted.turnId)
      || body.thread_id && accepted.threadId !== body.thread_id)) invalid();
  return { command, accepted, status: accepted ? 'accepted' : 'unknown' };
}

export function useSharedTaskSubmission({ enabled, projectId }) {
  const device = enabled ? readDeviceCredential()?.device?.device_id : null;
  const key = device ? userStorageKey(`shared-do:${encodeURIComponent(device)}:${projectId}`) : null;
  const actor = deviceIdentity(), gate = useRequestScope(`${key}:${actor.revision}`), scope = gate.scope;
  const [state, setState] = useState(null);
  const current = () => key && gate.isScopeCurrent(scope) && isDeviceIdentity(actor);
  const visible = state?.scope === scope ? state : null;
  function localEntry() {
    const entry = readEntry(key, projectId);
    return entry && !entry.accepted && visible?.entry?.command.id === entry.command.id
      ? { ...entry, accepted: visible.entry.accepted, status: visible.entry.status } : entry;
  }
  function refresh() {
    if (!current()) return null;
    try {
      const entry = localEntry();
      setState({ scope, entry });
      return entry;
    } catch (error) { setState({ scope, error: error.message }); return null; }
  }
  useEffect(() => { refresh(); }, [scope]);
  function prepare(body) {
    if (!current()) throw new Error('共享任务设备未核对');
    const entry = localEntry();
    if (entry) { setState({ scope, entry }); return null; }
    const command = { version: 1, storageKey: key, id: crypto.randomUUID(), projectId, path: endpoint,
      expected: { kind: 'shared-do' }, body, phase: 'prepared', createdAt: new Date().toISOString() };
    saveSharedCommand(command);
    setState({ scope, entry: { command, accepted: null, status: 'unknown' } });
    return command;
  }
  function acknowledge(command, accepted) {
    if (!current()) return;
    if (command.storageKey !== key || command.projectId !== projectId) invalid();
    const entry = { command, accepted, status: 'accepted' };
    const stored = readStoredJson(acceptedKey(command));
    if (stored.unavailable || stored.invalid || stored.value
        && (stored.value.commandId !== command.id || stored.value.threadId !== accepted.threadId
          || stored.value.turnId !== accepted.turnId)) invalid();
    setState({ scope, entry });
    if (!stored.value && !writeStoredJson(acceptedKey(command), { commandId: command.id, ...accepted }))
      throw new Error('原回执编号未保存 · 核对浏览器存储');
  }
  function settle(command) {
    if (!current()) return;
    if (command.storageKey !== key || command.projectId !== projectId) invalid();
    clearSharedCommand(command, { purgeRecord: true });
    removeStoredItem(acceptedKey(command));
    refresh();
  }
  function observe(command, status) {
    if (!current()) return;
    if (command.storageKey !== key || command.projectId !== projectId
        || !['unknown', 'preparing', 'not_received'].includes(status)) invalid();
    setState({ scope, entry: { command, accepted: null, status } });
  }
  return { visible, refresh, prepare, acknowledge, settle, observe };
}
