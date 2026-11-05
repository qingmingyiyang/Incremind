import { useCallback, useEffect, useRef, useState } from 'react';
import { deviceIdentity, isDeviceIdentity, readDeviceCredential, userStorageKey } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { sharedInspirationCommands, newSharedInspirationCommand, markSharedInspirationCommand } from './sharedInspirationCommands';
import { sharedIntakeApi } from './sharedIntakeApi';

export function sharedInspirationStorageKey(project) {
  const device = readDeviceCredential()?.device;
  if (!device?.device_id || !device.user_id) throw new Error('设备身份未核对 · 重新登录');
  return userStorageKey(`shared-intake-v1:${encodeURIComponent(device.device_id)}:shared:${encodeURIComponent(project)}:inspiration`);
}

export function useSharedInspirations({ enabled, canWrite, projectId }) {
  const [epoch, setEpoch] = useState(0), [state, setState] = useState(null), [working, setWorking] = useState(false);
  const [failure, setFailure] = useState(null);
  let key = null, keyError = null;
  if (enabled) { try { key = sharedInspirationStorageKey(projectId); } catch (error) { keyError = error.message; } }
  const gate = useRequestScope(`${key}:${epoch}:${enabled}`), scope = gate.scope, identity = deviceIdentity();
  const active = useRef(null), controller = useRef(null);
  const isWorking = useCallback(() => active.current?.scope === scope, [scope]);
  const current = token => enabled && key && gate.isCurrent(token) && isDeviceIdentity(identity)
    && sharedInspirationStorageKey(projectId) === key;
  const rows = state?.scope === scope ? state.commands : [];
  const values = state?.scope === scope ? state.values : {};
  const ready = Boolean(state?.scope === scope);
  function sync() {
    const commands = sharedInspirationCommands(key, projectId);
    setState(old => ({ scope, commands, values: old?.scope === scope ? old.values : {} }));
    return commands;
  }
  function receive(command, value, token) {
    if (!current(token)) return false;
    markSharedInspirationCommand(command, 'pending');
    const commands = sharedInspirationCommands(key, projectId);
    setState(old => ({ scope, commands, values: { ...(old?.scope === scope ? old.values : {}), [command.id]: value } }));
    return true;
  }
  async function operate(command, send = false) {
    if (active.current || !enabled || !key || send && !canWrite) return false;
    const token = gate.issue('operation', scope);
    if (!token || !isDeviceIdentity(identity)) return false;
    const network = new AbortController(); controller.current = network; active.current = token;
    setWorking(true); setFailure(null);
    try {
      if (send) { markSharedInspirationCommand(command, 'unknown'); sync(); }
      const value = await (send ? sharedIntakeApi.send(command, network.signal) : sharedIntakeApi.read(command, network.signal));
      return receive(command, value, token);
    } catch (error) {
      if (!current(token)) return false;
      if (!send && error.status === 404 && error.code === 'shared_intake_not_received') {
        try {
          markSharedInspirationCommand(command, 'not_received'); sync();
          setFailure({ scope, id: command.id, message: '尚未受理 · 可用原编号重试' });
        } catch (failure) { setFailure({ scope, id: command.id, message: failure.message }); }
      } else setFailure({ scope, id: command.id, message: error.message || '结果待核对' });
      return false;
    } finally {
      if (controller.current === network) controller.current = null;
      if (active.current === token) { active.current = null; if (gate.isScopeCurrent(scope)) setWorking(false); }
    }
  }
  useEffect(() => {
    controller.current?.abort(); active.current = null; setWorking(false); setState(null); setFailure(null);
    if (!enabled || !key) return;
    const token = gate.issue('restore', scope), network = new AbortController(); controller.current = network;
    let commands;
    try { commands = sync(); }
    catch (error) { setFailure({ scope, message: error.message }); return; }
    setWorking(true); active.current = token;
    async function restore() {
      for (const command of commands) {
        if (!current(token)) return;
        try { receive(command, await sharedIntakeApi.read(command, network.signal), token); }
        catch (error) {
          if (!current(token)) return;
          if (error.status === 404 && error.code === 'shared_intake_not_received') {
            markSharedInspirationCommand(command, 'not_received'); sync();
          } else setFailure({ scope, id: command.id, message: error.message || '结果待核对' });
        }
      }
    }
    restore().catch(error => {
      if (current(token)) setFailure({ scope, message: error.message || '灵感记录未保存 · 核对' });
    }).finally(() => {
      if (controller.current === network) controller.current = null;
      if (active.current === token) { active.current = null; if (gate.isScopeCurrent(scope)) setWorking(false); }
    });
    return () => { network.abort(); gate.invalidate('restore'); };
  }, [scope]);
  useEffect(() => {
    const changed = () => { controller.current?.abort(); gate.invalidate('operation'); gate.invalidate('restore'); setEpoch(value => value + 1); };
    window.addEventListener('chriptmas-user-space', changed); window.addEventListener('chriptmas-device-required', changed);
    return () => { controller.current?.abort(); window.removeEventListener('chriptmas-user-space', changed); window.removeEventListener('chriptmas-device-required', changed); };
  }, [gate]);
  async function send(text) {
    if (!canWrite || !ready || working || !key || active.current) return false;
    try {
      const commands = sharedInspirationCommands(key, projectId);
      const existing = commands.find(command => command.body.text === text && !values[command.id]);
      if (existing) {
        if (existing.phase === 'not_received') return operate(existing, true);
        await operate(existing);
        return false;
      }
      return operate(newSharedInspirationCommand(key, projectId, text), true);
    } catch (error) { if (gate.isScopeCurrent(scope)) setFailure({ scope, message: error.message }); return false; }
  }
  return { ready: !enabled || ready, working, isWorking, send, commands: rows, values,
    error: keyError || (failure?.scope === scope ? failure.message : ''),
    errorId: failure?.scope === scope ? failure.id : null,
    read: command => operate(command), retry: command => command.phase === 'not_received' ? operate(command, true) : operate(command),
    reload: () => { if (enabled && gate.isScopeCurrent(scope)) setEpoch(value => value + 1); },
  };
}
