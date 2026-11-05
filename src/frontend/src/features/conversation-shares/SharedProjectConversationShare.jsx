import { useEffect, useRef, useState } from 'react';
import { deviceIdentity, isDeviceIdentity, readDeviceCredential, userStorageKey } from '../../shared/api/deviceTransport';
import { readStoredJson, writeStoredJson } from '../../shared/lib/browserStorage';
import { projectScopeLabel } from '../../shared/lib/projectScope';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { StatusDot } from '../../shared/ui/StatusDot';
import { sharedProjectsApi } from '../shared-projects/sharedProjectsApi';
import { clearSharedCommand, readSharedCommands, saveSharedCommand } from '../shared-projects/sharedProjectCommands';
import { conversationShareApi as api } from './conversationShareApi';
import { conversationProjectCommand, conversationProjectNotAccepted, conversationProjectPointer,
  conversationProjectReceipt, conversationProjectTarget, conversationSharePreview, conversationSourceRevisionAdvanced,
  pendingConversationProjectCommand } from './conversationShareViews';

const changedSource = new Set(['conversation_source_revision_changed', 'conversation_citation_changed']);
const rejections = new Set([...changedSource, 'shared_authority_changed', 'conversation_citation_static',
  'conversation_source_unavailable', 'conversation_share_source_changed', 'conversation_citation_invalid', 'conversation_share_request_invalid']);
const permanentRejection = error => error.status === 409 && (changedSource.has(error.code) || error.code === 'shared_authority_changed')
  || error.status === 400 && ['conversation_citation_invalid', 'conversation_share_request_invalid'].includes(error.code);
const pointerFor = command => ({ id: command.id, projectId: command.expected.projectId, namespace: command.body.namespace });

export function useSharedProjectConversationShare({ source, blocked, onSourceChanged }) {
  const actor = deviceIdentity(), device = readDeviceCredential()?.device?.device_id;
  const key = device ? userStorageKey(`conversation-share-project:${[device, source.scopeKind, source.projectId, source.turnId]
    .map(encodeURIComponent).join(':')}`) : null;
  const gate = useRequestScope(`${key}:${actor.revision}`), scope = gate.scope;
  const [state, setState] = useState(null), [directory, setDirectory] = useState(null), [directoryEpoch, setDirectoryEpoch] = useState(0);
  const active = useRef(null), latestDirectory = useRef(null);
  const visible = state?.scope === scope ? state : null;
  const targets = directory?.scope === scope ? directory : null;
  latestDirectory.current = targets;
  const current = () => key && gate.isScopeCurrent(scope) && isDeviceIdentity(actor);
  const canWrite = id => latestDirectory.current?.scope === scope && latestDirectory.current.rows?.some(row => row.id === id);
  const reloadTargets = () => setDirectoryEpoch(value => value + 1);
  const latestKey = `${key}:latest`;
  const readPending = () => readSharedCommands(key).map(command => conversationProjectCommand(command, source));
  const refreshPending = () => {
    const pending = readPending();
    update({ pending });
    return pending;
  };
  function update(patch) {
    setState(previous => ({ ...(previous?.scope === scope ? previous : {}), scope, ...patch }));
  }
  useEffect(() => {
    const token = gate.issue('targets', scope), controller = new AbortController();
    if (!token || !current()) return;
    const allowed = () => gate.isCurrent(token) && current();
    setDirectory({ scope, loading: true });
    sharedProjectsApi.scopes(controller.signal).then(rows => {
      if (allowed()) setDirectory({ scope, rows: rows.filter(row => row.can_write === true) });
    }).catch(error => { if (allowed()) setDirectory({ scope, error: error.message }); });
    return () => { controller.abort(); gate.invalidate('targets'); };
  }, [scope, directoryEpoch]);
  async function run(operation, mutating = false) {
    if (!current() || active.current?.scope === scope) return;
    const token = gate.issue('command', scope), controller = new AbortController();
    if (!token) return;
    const activeOperation = { scope, controller };
    const live = () => gate.isCurrent(token) && current();
    active.current = activeOperation; update({ working: true, mutating, error: null });
    try { await operation(controller.signal, live); }
    catch (error) { if (live()) update({ error: error.message }); }
    finally {
      if (active.current === activeOperation) active.current = null;
      if (live()) update({ working: false, mutating: false });
    }
  }
  function receive(value, command) {
    const pointer = pointerFor(command), receipt = conversationProjectReceipt(value, pointer);
    if (!writeStoredJson(latestKey, pointer)) throw new Error('分享回执编号未保存 · 核对浏览器存储');
    clearSharedCommand(command);
    update({ pending: readPending(), command: null, phase: 'accepted', receipt });
  }
  async function inspect(command, signal, live, rejection) {
    const pointer = pointerFor(command);
    update({ command, phase: 'unknown' });
    const value = await api.projectCommand(pointer.projectId, pointer.id, pointer.namespace, signal);
    if (!live()) return false;
    if (!conversationProjectNotAccepted(value)) { receive(value, command); return false; }
    if (rejection) {
      let obsolete = false;
      if (rejection.status === 409 && rejection.code === 'conversation_share_source_changed') {
        try {
          const preview = conversationSharePreview(await api.preview(source, signal));
          obsolete = conversationSourceRevisionAdvanced(command.body.source_revision, preview.source_revision);
        } catch { /* 无法证明原修订已过期时保留原编号。 */ }
        if (!live()) return false;
        if (obsolete) {
          const latest = await api.projectCommand(pointer.projectId, pointer.id, pointer.namespace, signal);
          if (!live()) return false;
          if (!conversationProjectNotAccepted(latest)) { receive(latest, command); return false; }
        }
      }
      if (permanentRejection(rejection) || obsolete) {
        clearSharedCommand(command);
        update({ pending: readPending(), command: null, phase: null, error: rejection.message,
          sourceRefresh: obsolete || changedSource.has(rejection.code) });
        if (rejection.code === 'shared_authority_changed') reloadTargets();
      } else update({ phase: 'blocked', error: rejection.message });
      return false;
    }
    update({ phase: 'not_accepted' });
    return true;
  }
  async function send(command, signal, live) {
    update({ command, phase: 'unknown' });
    let result;
    try { result = await api.sendToProject(command.expected.projectId, command, signal); }
    catch (error) {
      if (!live()) return;
      if (!rejections.has(error.code)) throw error;
      await inspect(command, signal, live, error);
      return;
    }
    if (live()) receive(result, command);
  }
  function check(id) {
    return run(async (signal, live) => {
      const pending = refreshPending(), command = id ? pending.find(row => row.id === id) : pending[0];
      if (command) { await inspect(command, signal, live); return; }
      if (id) return;
      const stored = readStoredJson(latestKey);
      if (stored.invalid || stored.unavailable) throw new Error('原分享记录未读取');
      if (!stored.value) { update({ command: null, phase: null }); return; }
      const pointer = conversationProjectPointer(stored.value);
      const result = await api.projectCommand(pointer.projectId, pointer.id, pointer.namespace, signal);
      if (!live()) return;
      update({ command: null, phase: 'accepted', receipt: conversationProjectReceipt(result, pointer) });
    });
  }
  useEffect(() => {
    void check();
    return () => {
      if (active.current?.scope === scope) { active.current.controller.abort(); active.current = null; }
      gate.invalidate('command');
    };
  }, [scope]);
  function start(projectId, preview, ordinals) {
    if (blocked || visible?.sourceRefresh || !canWrite(projectId) || !preview) return;
    return run(async (signal, live) => {
      update({ receipt: null, phase: null });
      const target = conversationProjectTarget(await api.projectTarget(projectId, signal), projectId);
      if (!live() || !canWrite(projectId)) return;
      const pending = pendingConversationProjectCommand(refreshPending(), projectId, target.namespace);
      if (pending) { await inspect(pending, signal, live); return; }
      const command = conversationProjectCommand({
        version: 1, storageKey: key, id: crypto.randomUUID(), path: `/projects/${projectId}`,
        expected: { action: 'share-project', projectId }, phase: 'prepared', createdAt: new Date().toISOString(),
        body: { turn_id: source.turnId, project_id: source.projectId, scope_kind: source.scopeKind,
          source_revision: preview.source_revision, include_citations: ordinals, namespace: target.namespace, authority: target.authority },
      }, source);
      saveSharedCommand(command);
      refreshPending();
      await send(command, signal, live);
    }, true);
  }
  function retry() {
    const expected = visible?.command;
    if (blocked || visible?.phase !== 'not_accepted' || !expected || !canWrite(expected.expected.projectId)) return;
    return run(async (signal, live) => {
      const command = refreshPending().find(row => row.id === expected.id);
      if (!command || command.expected.projectId !== expected.expected.projectId || command.body.namespace !== expected.body.namespace) return;
      if (await inspect(command, signal, live) && live() && canWrite(command.expected.projectId)) await send(command, signal, live);
    }, true);
  }
  function open(receipt, onNavigate) {
    if (!current() || visible?.receipt !== receipt) return;
    onNavigate?.('library', { scope_kind: 'shared', project_id: receipt.projectId, layer: 'source', source_id: receipt.itemId });
  }
  function refreshSource() {
    if (!current() || visible?.working || !visible?.sourceRefresh) return;
    update({ sourceRefresh: false, error: null });
    onSourceChanged?.();
  }
  return { visible, targets, canWrite, reloadTargets, check, start, retry, open, scope, blocked, refreshSource };
}

export function SharedProjectConversationDestination({ state, preview, ordinals, disabled }) {
  const [selected, setSelected] = useState(null), targets = state.targets?.rows || [];
  const target = selected?.scope === state.scope && selected.revision === preview.source_revision ? selected.id : '';
  const hasOriginal = state.visible?.pending?.some(command => command.expected.projectId === target);
  if (!targets.length) return null;
  return <>
    <label>共享项目<select aria-label="共享目标项目" value={target} disabled={disabled}
      onChange={event => setSelected({ scope: state.scope, revision: preview.source_revision, id: event.target.value })}>
      <option value="">选择共享项目</option>{targets.map(row => <option key={row.id} value={row.id}>{projectScopeLabel(row, targets)}</option>)}
    </select></label>
    <button type="button" disabled={disabled || state.visible?.sourceRefresh || !state.canWrite(target)} onClick={() => {
      state.start(target, preview, ordinals); setSelected(null);
    }}>{hasOriginal ? '核对并分享至项目' : '分享至项目'}</button>
  </>;
}

export function SharedProjectConversationStatus({ state, onNavigate }) {
  const { visible, targets } = state;
  return <>
    {targets?.loading && <StatusDot state="processing"/>}
    {targets?.error && <p role="alert">{targets.error} · <button type="button" onClick={state.reloadTargets}>重读共享项目</button></p>}
    {visible?.pending?.map(command => {
      const project = command.expected.projectId, active = visible.command?.id === command.id;
      const phase = active ? visible.phase : 'unknown', target = targets?.rows?.find(row => row.id === project)?.name || project;
      return <div key={command.id} className="ui-conversation-error" role="status">
        <span>{target} · {phase === 'not_accepted' ? '原请求未受理' : phase === 'blocked' ? '原请求已保留' : '结果待核对'}</span>
        <button type="button" disabled={visible.working} onClick={() => state.check(command.id)}>核对原请求</button>
        {phase === 'not_accepted' && state.canWrite(project)
          && <button type="button" disabled={visible.working || state.blocked} onClick={state.retry}>继续原项目请求</button>}
      </div>;
    })}
    {visible?.working && <StatusDot state="processing"/>}
    {(visible?.error || visible?.sourceRefresh) && <p role="alert">{visible.error || '原对话快照已变化'}
      {visible.sourceRefresh && <button type="button" disabled={visible.working} onClick={state.refreshSource}>重新预览</button>}</p>}
    {visible?.receipt && !visible.command && !visible.working && <p role="status">已存为共享原件 {onNavigate && <button type="button"
      onClick={() => state.open(visible.receipt, onNavigate)}>打开共享原件</button>}</p>}
  </>;
}
