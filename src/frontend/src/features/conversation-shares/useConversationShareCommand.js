import { useEffect, useRef, useState } from 'react';
import { deviceIdentity, isDeviceIdentity, readDeviceCredential, userStorageKey } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { clearSharedCommand, readSharedCommand, saveSharedCommand } from '../shared-projects/sharedProjectCommands';
import { conversationShareApi as api } from './conversationShareApi';
import { conversationCommandShare, validateConversationCommand } from './conversationShareViews';

const terminalRejections = new Set([
  'conversation_source_revision_changed', 'conversation_share_revision_changed', 'conversation_share_already_filed',
  'conversation_share_cannot_file',
  'conversation_source_unavailable', 'conversation_recipient_unavailable', 'conversation_archive_project_unavailable',
  'conversation_share_request_invalid', 'conversation_citation_invalid',
]);

export function useConversationShareCommand({ enabled, source, onChanged }) {
  const actor = deviceIdentity(), device = enabled ? readDeviceCredential()?.device?.device_id : null;
  const surface = source ? [source.scopeKind, source.projectId, source.turnId].map(encodeURIComponent).join(':') : 'inbox';
  const key = device ? userStorageKey(`conversation-share:${encodeURIComponent(device)}:${surface}`) : null;
  const gate = useRequestScope(`${key}:${actor.revision}`), scope = gate.scope;
  const active = useRef(null), changed = useRef(onChanged), [state, setState] = useState(null);
  changed.current = onChanged;
  const visible = state?.scope === scope ? state : null;
  const working = Boolean(visible?.working), current = () => key && gate.isScopeCurrent(scope) && isDeviceIdentity(actor);
  const read = () => {
    const command = readSharedCommand(key);
    return command && validateConversationCommand(command, source);
  };
  function settle(command, error) {
    clearSharedCommand(command);
    const next = read();
    setState({ scope, command: next, phase: next ? 'unknown' : 'idle', error });
  }
  async function operate(command, send) {
    if (!command || !current() || active.current?.scope === scope) return;
    validateConversationCommand(command, source);
    const token = gate.issue('command', scope);
    if (!token) return;
    const controller = new AbortController(), operation = { scope, controller };
    const allowed = () => current() && gate.isCurrent(token);
    active.current = operation;
    setState({ scope, command, phase: 'unknown', working: true });
    function receive(value) {
      const share = conversationCommandShare(value, command, source);
      if (share.state === 'filing' && command.expected.action === 'file')
        setState({ scope, command, phase: 'filing' });
      else settle(command, share.state === 'revoked' && command.expected.action === 'file' ? '对话分享已撤回' : undefined);
      changed.current?.(share);
    }
    function observe(value, rejection) {
      if (value?.state === 'not_accepted') {
        if (rejection) { settle(command, rejection.message); changed.current?.(); }
        else setState({ scope, command, phase: 'not_accepted' });
        return;
      }
      if (value?.action !== command.expected.action) throw new Error('原操作身份未核对');
      receive(value.share);
    }
    try {
      if (send) {
        let result;
        try { result = await api.execute(command, controller.signal); }
        catch (error) {
          if (!allowed()) return;
          if (!terminalRejections.has(error.code)) throw error;
          const proof = await api.command(command.id, controller.signal);
          if (allowed()) observe(proof, error);
          return;
        }
        if (allowed()) receive(result);
      } else {
        const result = await api.command(command.id, controller.signal);
        if (allowed()) observe(result);
      }
    } catch (error) {
      if (allowed()) setState({ scope, command, phase: 'unknown', error: error.message });
    } finally { if (active.current === operation) active.current = null; }
  }
  function check() {
    if (!current() || active.current?.scope === scope) return;
    try {
      const command = read();
      if (command) void operate(command, false);
      else setState({ scope, phase: 'idle' });
    } catch (error) { setState({ scope, error: error.message }); }
  }
  useEffect(() => {
    check();
    return () => {
      if (active.current?.scope === scope) { active.current.controller.abort(); active.current = null; }
      gate.invalidate('command');
    };
  }, [scope]);
  function start(action, body, shareId, originalKey) {
    if (!current() || active.current?.scope === scope) return;
    try {
      const previous = read();
      if (previous) { void operate(previous, false); return; }
      const command = validateConversationCommand({
        version: 1, storageKey: key, id: originalKey || crypto.randomUUID(), phase: 'prepared',
        path: action === 'send' ? '' : `/${shareId}/${action}`, expected: { action, shareId },
        body, createdAt: new Date().toISOString(),
      }, source);
      saveSharedCommand(command);
      void operate(command, !originalKey);
    } catch (error) { setState({ scope, error: error.message }); }
  }
  function retry() {
    if (!current() || active.current?.scope === scope || !['not_accepted', 'filing'].includes(visible?.phase)) return;
    try {
      const command = read();
      if (command?.id === visible.command?.id) void operate(command, true);
    } catch (error) { setState({ scope, error: error.message }); }
  }
  return { visible, working, check, start, retry };
}
