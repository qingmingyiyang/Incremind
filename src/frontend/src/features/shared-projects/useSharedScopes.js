import { useEffect, useState } from 'react';
import { deviceIdentity, isDeviceIdentity, userStorageKey } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { devicesApi } from '../settings/devicesApi';
import { sharedProjectsApi } from './sharedProjectsApi';

export function useSharedScopes(userEpoch, entry) {
  const key = userStorageKey('shared-scopes'), gate = useRequestScope(`${key}:${userEpoch}`), scope = gate.scope;
  const [result, setResult] = useState(null), [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const token = gate.issue('read', scope), actor = deviceIdentity(), controller = new AbortController();
    const current = () => gate.isCurrent(token) && isDeviceIdentity(actor) && userStorageKey('shared-scopes') === key;
    async function read() {
      if (!current()) return;
      try {
        const deployment = await devicesApi.list();
        if (!current()) return;
        if (!['server', 'desktop'].includes(deployment?.mode)) throw new Error('运行形态未核对');
        const items = deployment.mode === 'server' ? await sharedProjectsApi.scopes(controller.signal) : [];
        if (!current()) return;
        setResult({ scope, items, server: deployment.mode === 'server', error: '' });
      } catch (reason) {
        if (!current()) return;
        setResult({ scope, items: [], error: reason.message });
      }
    }
    read();
    const changed = () => setRefresh(value => value + 1);
    window.addEventListener('focus', changed);
    return () => { controller.abort(); gate.invalidate('read'); window.removeEventListener('focus', changed); };
  }, [scope, refresh, entry]);
  return { ...(result?.scope === scope ? result : { items: [], error: '', loading: true }), reload: () => setRefresh(value => value + 1) };
}
