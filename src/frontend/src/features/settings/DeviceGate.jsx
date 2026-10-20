import { Fragment, useEffect, useState } from 'react';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { saveDeviceCredential, readUserSpace, selectUserSpace, userStorageKey } from '../../shared/api/deviceTransport';
import { devicesApi } from './devicesApi';
import { usersApi } from './usersApi';
import './Settings.css';
import { UserSpaceHeader } from '../../shared/ui/UserSpaceHeader';

function takeCode() {
  if (location.pathname !== '/pair') return '';
  const params = new URLSearchParams(location.hash.slice(1)), code = params.get('code') || '';
  if (params.has('code')) {
    params.delete('code');
    const hash = params.toString();
    history.replaceState(null, '', location.pathname + location.search + (hash ? '#' + hash : ''));
  }
  return code;
}

export function DeviceGate({ children }) {
  const [code, setCode] = useState(takeCode), [name, setName] = useState('');
  const [state, setState] = useState(globalThis.electronAPI?.backendBaseUrl ? 'ready' : 'checking');
  const [error, setError] = useState(''), [busy, setBusy] = useState(false), [retry, setRetry] = useState(0);
  const gate = useRequestScope('device-bootstrap'), scope = gate.scope;
  const [space,setSpace] = useState(readUserSpace);
  const [server, setServer] = useState(false), [caller, setCaller] = useState(null);
  useEffect(()=>{
    const changed=()=>{
      history.replaceState(null,'',location.pathname+'#view=workbench&project_id=default');
      setSpace(readUserSpace());
      window.dispatchEvent(new Event('hashchange'));
    };
    window.addEventListener('chriptmas-user-space',changed);
    return()=>window.removeEventListener('chriptmas-user-space',changed);
  },[]);
  useEffect(() => {
    if (globalThis.electronAPI?.backendBaseUrl) return;
    const token = gate.issue('probe', scope);
    devicesApi.list().then(value => { if (gate.isCurrent(token)) { setServer(value.mode === 'server'); setState('ready'); } })
      .catch(reason => { if (gate.isCurrent(token)) { const pairing = reason.status === 401; setState(pairing ? 'pair' : 'error'); setError(pairing ? '' : '读取未完成 · 重试'); } });
    return () => gate.invalidate('probe');
  }, [retry]);
  useEffect(() => {
    if (state !== 'ready' || !server) return;
    const token = gate.issue('caller', scope);
    usersApi.list().then(value => { if (gate.isCurrent(token)) setCaller(value.caller); }).catch(() => {});
    return () => gate.invalidate('caller');
  }, [state, server]);
  useEffect(() => {
    const required = () => { gate.invalidate('probe'); gate.invalidate('exchange'); gate.invalidate('caller'); setCaller(null); setSpace(null); setState('pair'); setBusy(false); setError(''); };
    window.addEventListener('chriptmas-device-required', required);
    return () => window.removeEventListener('chriptmas-device-required', required);
  }, []);
  async function exchange(event) {
    event.preventDefault(); if (busy || !code.trim() || !name.trim()) return;
    const token = gate.issue('exchange', scope);
    setBusy(true); setError('');
    try {
      const value = await devicesApi.exchange(code.trim(), name.trim());
      if (gate.isCurrent(token)) { saveDeviceCredential(value); setCode(''); setState('checking'); setRetry(old => old + 1); }
    } catch { if (gate.isCurrent(token)) setError('配对未完成 · 重试'); }
    finally { if (gate.isCurrent(token)) setBusy(false); }
  }
  function returnToManagement() {
    selectUserSpace(null);
    history.replaceState(null, '', location.pathname + '#view=settings&project_id=default&section=devices');
    window.dispatchEvent(new Event('hashchange'));
  }
  const headerStart = server ? <div className="ui-shell-space-context">
    <span className="ui-shell-space-name" title={`${space?.name || caller?.name || '自己'}的空间`}>{space?.name || caller?.name || '自己'}的空间</span>
    {space && <button type="button" aria-label={`${space.name} · 返回自己的空间`} onClick={() => selectUserSpace(null)}>返回自己</button>}
    {(space || caller?.role === 'admin') && <button type="button" aria-label="返回用户管理" onClick={returnToManagement}>用户管理</button>}
  </div> : null;
  if (state === 'ready') return <UserSpaceHeader.Provider value={headerStart}>
    <Fragment key={userStorageKey('view')}>{children}</Fragment>
  </UserSpaceHeader.Provider>;
  return <div className="device-pair-page"><main className="settings-page"><section>
    <h1>设备配对</h1>{state === 'checking' ? <span role="status">读取中</span> : state === 'error'
      ? <p role="alert">{error} <button type="button" onClick={() => { setState('checking'); setRetry(value => value + 1); }}>重试</button></p>
      : <form onSubmit={exchange}><label>配对码<input aria-label="配对码" type="password" autoComplete="off" value={code} onChange={event => setCode(event.target.value)} disabled={busy}/></label>
        <label>名称<input aria-label="设备名称" maxLength={80} value={name} onChange={event => setName(event.target.value)} disabled={busy}/></label>
        {error && <p role="alert">{error}</p>}<button type="submit" disabled={busy || !code.trim() || !name.trim()}>配对</button></form>}
  </section></main></div>;
}
