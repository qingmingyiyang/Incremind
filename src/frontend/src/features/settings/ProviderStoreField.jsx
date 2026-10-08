import { useEffect, useRef, useState } from 'react';
import { Row, Switch } from '../../shared/ui';
import { userStorageKey } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { providerStoreApi } from './providerStoreApi';

export function ProviderStoreField({ projectId, mode, generationRevision, modeRevision, busy, api = providerStoreApi }) {
  const [userSpace, setUserSpace] = useState(() => userStorageKey('provider-store'));
  const gate = useRequestScope(`${userSpace}:${projectId}:${mode}:${generationRevision}:${modeRevision}`), scope = gate.scope;
  const [loaded, setLoaded] = useState(null), [error, setError] = useState('');
  const [saving, setSaving] = useState(false), [refresh, setRefresh] = useState(0);
  const writeController = useRef(null);
  useEffect(() => {
    const changed = () => setUserSpace(userStorageKey('provider-store'));
    window.addEventListener('chriptmas-user-space', changed);
    window.addEventListener('chriptmas-device-required', changed);
    return () => {
      window.removeEventListener('chriptmas-user-space', changed);
      window.removeEventListener('chriptmas-device-required', changed);
    };
  }, []);
  useEffect(() => {
    setLoaded(null); setError(''); setSaving(false);
    if (mode !== 'api') return undefined;
    const token = gate.issue('load', scope), controller = new AbortController();
    api.load(controller.signal).then(value => {
      if (gate.isCurrent(token)) setLoaded({ scope, value });
    }).catch(() => { if (gate.isCurrent(token)) setError('读取未完成 · 重试'); });
    return () => {
      controller.abort(); writeController.current?.abort();
      gate.invalidate('load'); gate.invalidate('write');
    };
  }, [api, mode, scope, refresh]);
  const value = loaded?.scope === scope ? loaded.value : null;
  async function save(enabled) {
    if (busy || saving || !value?.available) return;
    const token = gate.issue('write', scope); if (!token) return;
    const controller = new AbortController(); writeController.current = controller;
    setSaving(true); setError('');
    try {
      // 三个修订来自同一次能力读取，失败时保持原选择，不预先开启。
      const updated = await api.save(enabled, value, controller.signal);
      if (gate.isCurrent(token)) setLoaded({ scope, value: updated });
    } catch (reason) {
      if (gate.isCurrent(token)) setError(reason.status === 409 || reason.code === 'revision_conflict'
        ? '设置已变化 · 刷新' : '操作未完成 · 重试');
    } finally {
      if (gate.isCurrent(token)) { setSaving(false); writeController.current = null; }
    }
  }
  if (mode !== 'api') return null;
  return <>
    {value?.available === true && <Row readOnly title="服务商暂存" trailing={<Switch label="服务商暂存"
      title="服务商暂存请求与回答，用于断网后续读；首字可能更慢"
      checked={value.enabled === true} disabled={busy || saving} onChange={save}/>}/>}
    {error && <p role="alert">{error} <button type="button" disabled={busy || saving}
      onClick={() => setRefresh(old => old + 1)}>刷新</button></p>}
  </>;
}
