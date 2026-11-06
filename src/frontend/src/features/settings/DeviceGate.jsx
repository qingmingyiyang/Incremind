import { Fragment, useEffect, useRef, useState } from 'react';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { saveDeviceCredential, saveMobileDeviceSession, isAndroidWorkbench, readUserSpace, selectUserSpace, userStorageKey, deviceIdentity, isDeviceIdentity, forgetDeviceCredential } from '../../shared/api/deviceTransport';
import { devicesApi } from './devicesApi';
import { usersApi } from './usersApi';
import './Settings.css';
import { UserSpaceHeader } from '../../shared/ui/UserSpaceHeader';
import { resumeAuthenticatedPage } from './loginNavigation';

const PAIRING_MESSAGES = {
  unavailable: '配对码已失效。可在已登录设备的设置 · 设备中生成新码，或联系管理员。',
  name: '设备名称无效。输入 1 至 80 个字符。',
  retry: '连接未完成。保留了配对信息，可以重试。',
};

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
  const [mobile, setMobile] = useState(isAndroidWorkbench);
  const [code, setCode] = useState(takeCode), [name, setName] = useState('');
  const [pairing, setPairing] = useState(() => location.pathname === '/pair');
  const [loginName, setLoginName] = useState(''), [password, setPassword] = useState('');
  const [state, setState] = useState(globalThis.electronAPI?.backendBaseUrl ? 'ready' : 'checking');
  const [error, setError] = useState(''), [busy, setBusy] = useState(false), [retry, setRetry] = useState(0);
  const [pairingIssue, setPairingIssue] = useState('');
  const pairingCode = useRef(null), deviceName = useRef(null);
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
    devicesApi.list().then(value => {
      if (!gate.isCurrent(token)) return;
      if (value.mobile_session) {
        saveMobileDeviceSession(value.mobile_session);
        setMobile(true);
      } else if (mobile) {
        setState('mobile-required');
        return;
      }
      setServer(value.mode === 'server'); setState('ready');
    }).catch(reason => {
      if (!gate.isCurrent(token)) return;
      if (reason.status === 401 || mobile && reason.status === 403) {
        setState(mobile ? 'mobile-required' : 'pair'); setError('');
      } else {
        setState('error'); setError('读取未完成 · 重试');
      }
    });
    return () => gate.invalidate('probe');
  }, [retry]);
  useEffect(() => {
    if (state !== 'ready' || !server) return;
    const token = gate.issue('caller', scope);
    usersApi.list().then(value => { if (gate.isCurrent(token)) setCaller(value.caller); }).catch(() => {});
    return () => gate.invalidate('caller');
  }, [state, server]);
  useEffect(() => {
    const required = () => { gate.invalidate('probe'); gate.invalidate('exchange'); gate.invalidate('login'); gate.invalidate('logout'); gate.invalidate('caller'); setCaller(null); setSpace(null); setState(mobile ? 'mobile-required' : 'pair'); setBusy(false); setPassword(''); setError(''); setPairingIssue(''); };
    window.addEventListener('chriptmas-device-required', required);
    return () => window.removeEventListener('chriptmas-device-required', required);
  }, [mobile]);
  useEffect(() => {
    if (busy || !pairing) return;
    if (pairingIssue === 'unavailable') pairingCode.current?.focus();
    if (pairingIssue === 'name') deviceName.current?.focus();
  }, [busy, pairing, pairingIssue]);
  async function exchange(event) {
    event.preventDefault(); if (busy || !code.trim() || !name.trim()) return;
    const token = gate.issue('exchange', scope), identity = deviceIdentity();
    setBusy(true); setError(''); setPairingIssue('');
    try {
      const value = await devicesApi.exchange(code.trim(), name.trim());
      if (gate.isCurrent(token) && isDeviceIdentity(identity)) { saveDeviceCredential(value); setCode(''); setState('checking'); setRetry(old => old + 1); }
    } catch (reason) {
      if (gate.isCurrent(token) && isDeviceIdentity(identity)) {
        if (reason.code === 'pairing_unavailable') {
          setCode(''); setPairingIssue('unavailable');
        } else setPairingIssue(reason.code === 'device_name_invalid' ? 'name' : 'retry');
      }
    }
    finally { if (gate.isCurrent(token)) setBusy(false); }
  }
  async function login(event) {
    event.preventDefault(); if (busy || !loginName.trim() || !password) return;
    const token = gate.issue('login', scope), identity = deviceIdentity();
    setBusy(true); setError('');
    try {
      const value = await devicesApi.login(loginName.trim(), password);
      if (gate.isCurrent(token) && isDeviceIdentity(identity)) {
        saveDeviceCredential(value); setPassword(''); setState('checking'); setRetry(old => old + 1);
        resumeAuthenticatedPage();
      }
    } catch (reason) {
      if (gate.isCurrent(token) && isDeviceIdentity(identity)) setError(reason.status === 429 ? '尝试过多 · 稍后重试' : reason.status === 401 ? '账号或密码不正确' : '登录未完成 · 重试');
    } finally { if (gate.isCurrent(token)) setBusy(false); }
  }
  async function logout() {
    if (busy) return;
    setPairing(false);
    const token = gate.issue('logout', scope), identity = deviceIdentity();
    setBusy(true); setError('');
    try {
      await devicesApi.logout();
      if (gate.isCurrent(token) && isDeviceIdentity(identity)) { setPairing(false); forgetDeviceCredential(identity); }
    } catch (reason) {
      if (gate.isCurrent(token) && isDeviceIdentity(identity)) {
        if (reason.status === 401) { setPairing(false); forgetDeviceCredential(identity); }
        else setError('退出未完成 · 重试');
      }
    } finally { if (gate.isCurrent(token)) setBusy(false); }
  }
  function returnToManagement() {
    if (readUserSpace()) selectUserSpace(null);
    history.replaceState(null, '', location.pathname + '#view=settings&project_id=default&section=devices');
    window.dispatchEvent(new Event('hashchange'));
  }
  const headerStart = server ? <div className="ui-shell-space-context">
    <span className="ui-shell-space-name" title={`${space?.name || caller?.name || '自己'}的空间`}>{space?.name || caller?.name || '自己'}的空间</span>
    {space && <button type="button" aria-label={`${space.name} · 返回自己的空间`} onClick={() => selectUserSpace(null)}>返回自己</button>}
    {(space || caller?.role === 'admin') && <button type="button" aria-label="返回用户管理" onClick={returnToManagement}>用户管理</button>}
    <button type="button" onClick={logout} disabled={busy}>退出登录</button>
    {error && <span role="alert">{error}</span>}
  </div> : null;
  if (state === 'ready') return <UserSpaceHeader.Provider value={headerStart}>
    <Fragment key={userStorageKey('view')}>{children}</Fragment>
  </UserSpaceHeader.Provider>;
  return <div className="device-pair-page"><main className="settings-page"><section>
    <h1>{mobile ? 'Android 设备' : pairing ? '设备配对' : '登录'}</h1>{state === 'checking' ? <span role="status">读取中</span> : state === 'error'
      ? <p role="alert">{error} <button type="button" onClick={() => { setState('checking'); setRetry(value => value + 1); }}>重试</button></p>
      : mobile ? <p role="status">请在 App 内扫描邀请二维码</p>
      : pairing ? <form onSubmit={exchange} aria-busy={busy}>
        {pairingIssue && <p role="alert" id="device-pairing-message">{PAIRING_MESSAGES[pairingIssue]}</p>}
        <label>配对码<input ref={pairingCode} aria-label="配对码" type="password" autoComplete="off" autoCapitalize="none" spellCheck={false}
          aria-invalid={pairingIssue === 'unavailable' || undefined} aria-describedby={pairingIssue === 'unavailable' ? 'device-pairing-message' : undefined}
          value={code} onChange={event => { setCode(event.target.value); setPairingIssue(''); }} disabled={busy}/></label>
        <label>名称<input ref={deviceName} aria-label="设备名称" maxLength={80}
          aria-invalid={pairingIssue === 'name' || undefined} aria-describedby={pairingIssue === 'name' ? 'device-pairing-message' : undefined}
          value={name} onChange={event => { setName(event.target.value); setPairingIssue(''); }} disabled={busy}/></label>
        <button type="submit" disabled={busy || !code.trim() || !name.trim()}>{busy ? '配对中' : pairingIssue === 'retry' ? '重试' : '配对'}</button>
        <button type="button" disabled={busy} onClick={() => { setPairing(false); setCode(''); setError(''); setPairingIssue(''); }}>账号登录</button></form>
      : <form onSubmit={login}><label>账号<input aria-label="账号" autoComplete="username" autoCapitalize="none" maxLength={64} value={loginName} onChange={event => setLoginName(event.target.value)} disabled={busy}/></label>
        <label>密码<input aria-label="密码" type="password" autoComplete="current-password" maxLength={512} value={password} onChange={event => setPassword(event.target.value)} disabled={busy}/></label>
        {error && <p role="alert">{error}</p>}<button type="submit" disabled={busy || !loginName.trim() || !password}>登录</button>
        <button type="button" disabled={busy} onClick={() => { setPairing(true); setPassword(''); setError(''); }}>设备配对</button></form>}
  </section></main></div>;
}
