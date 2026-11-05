import { useCallback, useEffect, useRef, useState } from 'react';
import { deviceIdentity, isDeviceIdentity, readDeviceCredential, userStorageKey } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { readScopedTurn, readThreadHistory } from '../../shared/lib/threadHistory';
import { workbenchApi, workbenchErrorMessage } from '../workbench/workbenchApi';
import { clearSharedCommand, readSharedCommand, saveSharedCommand } from './sharedProjectCommands';

const validId = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/.test(value);
const invalid = () => { throw new Error('原记住记录未核对 · 重读'); };

export function sharedRememberResult(value, projectId, threadId) {
  const turn = readScopedTurn(value?.turn, 'shared'), memory = turn.receipt?.remember;
  readThreadHistory({ id: value.thread_id, scope_kind: 'shared', turns: [turn] }, value.thread_id, projectId, 'shared');
  if (threadId && value.thread_id !== threadId || turn.intent !== 'remember' || !validId(memory?.item_id)
      || !['processing', 'done', 'failed'].includes(memory.state) || typeof memory.title !== 'string'
      || memory.state === 'done' && !validId(memory.document_id)) invalid();
  return value;
}

export function sharedRememberRecovery(response, body) {
  if (!['preparing', 'failed'].includes(response.state)) return false;
  if (typeof response.recoverable !== 'boolean') invalid();
  if (!response.recoverable) return false;
  const original = response.request;
  const fields = Object.keys(body);
  if (!original || typeof original !== 'object' || Array.isArray(original)
      || fields.length !== Object.keys(original).length
      || fields.some(field => !Object.hasOwn(original, field) || body[field] !== original[field])) invalid();
  return true;
}

function storedCommand(key, projectId) {
  const command = readSharedCommand(key);
  if (!command) return null;
  const { body } = command;
  if (!validId(command.id) || command.projectId !== projectId || command.expected.kind !== 'shared-remember'
      || command.path !== '/api/v2/workbench/turns' || body.project_id !== projectId || body.scope_kind !== 'shared'
      || body.intent !== 'remember' || typeof body.text !== 'string' || !body.text.trim()
      || body.thread_id !== undefined && !validId(body.thread_id)
      || Object.keys(body).some(field => !['project_id', 'scope_kind', 'intent', 'text', 'thread_id'].includes(field))) invalid();
  return command;
}

export function useSharedRemember({ enabled, canWrite, projectId }) {
  const device = enabled ? readDeviceCredential()?.device?.device_id : null;
  const key = device ? userStorageKey(`shared-remember:${encodeURIComponent(device)}:${projectId}`) : null;
  const actor = deviceIdentity(), gate = useRequestScope(`${key}:${actor.revision}`), scope = gate.scope;
  const [state, setState] = useState(null), [working, setWorking] = useState(false);
  const active = useRef(null);
  const isWorking = useCallback(() => active.current?.scope === scope, [scope]);
  const visible = state?.scope === scope ? state : null;
  const current = token => gate.isCurrent(token) && isDeviceIdentity(actor);

  async function operate(command, send) {
    if (!key || active.current || !isDeviceIdentity(actor) || send && !canWrite) return null;
    const token = gate.issue('remember', scope);
    if (!token) return null;
    const controller = new AbortController(), operation = { controller, scope };
    active.current = operation; setWorking(true);
    setState({ scope, command, status: 'checking' });
    try {
      let response;
      if (send) {
        const result = await workbenchApi.create(command.body, { submissionKey: command.id, signal: controller.signal });
        response = { state: 'accepted', project_id: projectId, scope_kind: 'shared', result };
      } else response = await workbenchApi.rememberCommand(projectId, command.id, { signal: controller.signal });
      if (!current(token)) return null;
      if (response?.project_id !== projectId || response.scope_kind !== 'shared'
          || !['accepted', 'preparing', 'failed'].includes(response.state)) invalid();
      const result = response.state === 'accepted' ? sharedRememberResult(response.result, projectId, command.body.thread_id) : null;
      if (response.state === 'failed' && typeof response.error !== 'string') invalid();
      const canResume = sharedRememberRecovery(response, command.body);
      setState({ scope, command, status: response.state, result, canResume,
        error: response.error && workbenchErrorMessage(response.error) });
      return send && result ? { command, result } : null;
    } catch (failure) {
      if (!current(token)) return null;
      const missing = !send && failure.status === 404 && failure.code === 'shared_remember_not_received';
      setState({ scope, command, status: missing ? 'not_received' : 'unknown', canResume: missing,
        error: missing ? '' : failure.message });
      return null;
    } finally {
      if (active.current === operation) {
        active.current = null;
        if (gate.isScopeCurrent(scope)) setWorking(false);
      }
    }
  }

  function check() {
    if (!key || active.current || !gate.isScopeCurrent(scope) || !isDeviceIdentity(actor)) return;
    try {
      const command = storedCommand(key, projectId);
      if (command) void operate(command, false);
      else setState({ scope, status: 'idle' });
    } catch (failure) { setState({ scope, status: 'storage_error', error: failure.message }); }
  }
  useEffect(() => {
    setWorking(false); check();
    return () => {
      if (active.current?.scope === scope) { active.current.controller.abort(); active.current = null; }
      gate.invalidate('remember');
    };
  }, [scope]);

  async function send(body) {
    if (!key || !canWrite || active.current || !gate.isScopeCurrent(scope) || !isDeviceIdentity(actor)) return null;
    try {
      const previous = storedCommand(key, projectId);
      if (previous) {
        void operate(previous, false); return null;
      }
      const command = { version: 1, storageKey: key, id: crypto.randomUUID(), projectId,
        path: '/api/v2/workbench/turns', expected: { kind: 'shared-remember' },
        body, phase: 'prepared', createdAt: new Date().toISOString() };
      saveSharedCommand(command);
      return operate(command, true);
    } catch (failure) { setState({ scope, status: 'storage_error', error: failure.message }); return null; }
  }
  function settle(command) {
    if (!gate.isScopeCurrent(scope) || !isDeviceIdentity(actor)) return;
    try { clearSharedCommand(command); setState({ scope, status: 'idle' }); }
    catch (failure) { setState({ scope, command, status: 'unknown', error: failure.message }); }
  }
  return { visible, working, isWorking, send, check, settle,
    retry: () => visible?.command && visible.canResume === true ? operate(visible.command, true) : null };
}
