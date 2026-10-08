import { Fragment, useEffect, useState } from 'react';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { saveDeviceCredential, readUserSpace, selectUserSpace, userStorageKey } from '../../shared/api/deviceTransport';
import { devicesApi } from './devicesApi';
import './Settings.css';
import { UserSpaceHeader } from '../../shared/ui/UserSpaceHeader';

function takeCode() {
  if (location.pathname !== '/pair') return '';
  const code = new URLSearchParams(location.hash.slice(1)).get('code') || '';
  if (location.hash) history.replaceState(null, '', location.pathname);
  return code;
}

export function DeviceGate({ children }) {
  const [code, setCode] = useState(takeCode), [name, setName] = useState('');
  const [state, setState] = useState(globalThis.electronAPI?.backendBaseUrl ? 'ready' : 'checking');
  const [error, setError] = useState(''), [busy, setBusy] = useState(false), [retry, setRetry] = useState(0);
  const gate = useRequestScope('device-bootstrap'), scope = gate.scope;
  const [space,setSpace] = useState(readUserSpace);
  useEffect(()=>{
    const changed=()=>{
      history.replaceState(null,'',location.pathname+'#view=workbench&project_id=default');
      setSpace(readUserSpace());
    };
    window.addEventListener('chriptmas-user-space',changed);
    return()=>window.removeEventListener('chriptmas-user-space',changed);
  },[]);
  useEffect(() => {
    if (globalThis.electronAPI?.backendBaseUrl) return;
    const token = gate.issue('probe', scope);
    devicesApi.list().then(() => { if (gate.isCurrent(token)) setState('ready'); })
      .catch(reason => { if (gate.isCurrent(token)) { setState(reason.status === 401 ? 'pair' : 'error'); setError('读取未完成 · 重试'); } });
    return () => gate.invalidate('probe');
  }, [retry]);
  useEffect(() => {
    const required = () => { gate.invalidate('probe'); gate.invalidate('exchange'); setState('pair'); setBusy(false); setError(''); };
    window.addEventListener('chriptmas-device-required', required);
    return () => window.removeEventListener('chriptmas-device-required', required);
  }, []);
  async function exchange(event) {
    event.preventDefault(); if (busy || !code.trim() || !name.trim()) return;
    const token = gate.issue('exchange', scope);
    setBusy(true); setError('');
    try {
      const value = await devicesApi.exchange(code.trim(), name.trim());
      if (gate.isCurrent(token)) { saveDeviceCredential(value); setCode(''); setState('ready'); }
    } catch { if (gate.isCurrent(token)) setError('配对未完成 · 重试'); }
    finally { if (gate.isCurrent(token)) setBusy(false); }
  }
  const headerStart = space ? <button type="button" className="ui-shell-user-space" aria-label={`${space.name} · 返回自己的空间`} title={space.name} onClick={()=>selectUserSpace(null)}>{Array.from(space.name || '·')[0]}</button> : null;
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
