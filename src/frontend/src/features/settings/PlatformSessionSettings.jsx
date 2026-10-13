import { useEffect, useRef, useState } from 'react';
import { Row, StatusDot } from '../../shared/ui';
import { userStorageKey } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { platformSessionApi } from './platformSessionApi';
import './PlatformSessionSettings.css';

const names = { bilibili: 'B站', xiaohongshu: '小红书' };
const states = { waiting: '待扫码', scanned: '待确认', expired: '已过期', saved: '已保存' };
export function PlatformSessionSettings({ api = platformSessionApi }) {
  const [space, setSpace] = useState(() => userStorageKey('platform-sessions'));
  useEffect(() => {
    const changed = () => setSpace(userStorageKey('platform-sessions'));
    window.addEventListener('chriptmas-user-space', changed);
    window.addEventListener('chriptmas-device-required', changed);
    return () => {
      window.removeEventListener('chriptmas-user-space', changed);
      window.removeEventListener('chriptmas-device-required', changed);
    };
  }, []);
  return <PlatformRows key={space} api={api} space={space}/>;
}

function PlatformRows({ api, space }) {
  const gate = useRequestScope(space), scope = gate.scope;
  const [rows, setRows] = useState(null), [open, setOpen] = useState(null);
  const [content, setContent] = useState(''), [error, setError] = useState('');
  const [busy, setBusy] = useState(false), [login, setLogin] = useState(null), [refresh, setRefresh] = useState(0);
  const writer = useRef(null);
  useEffect(() => {
    const controller = new AbortController(), token = gate.issue('load', scope);
    api.load(controller.signal).then(value => { if (gate.isCurrent(token)) setRows(value.items); })
      .catch(() => { if (gate.isCurrent(token)) setError('读取未完成 · 刷新'); });
    return () => { controller.abort(); writer.current?.abort(); };
  }, [api, scope, refresh]);
  useEffect(() => {
    if (!login || !['waiting', 'scanned'].includes(login.state)) return undefined;
    const controller = new AbortController(), token = gate.issue('poll', scope);
    const timer = setTimeout(async () => {
      try {
        const result = await api.poll(login.login_id, controller.signal);
        if (gate.isCurrent(token)) {
          setLogin(old => ({ ...old, ...result }));
          if (result.state === 'saved') { setRefresh(old => old + 1); setContent(''); }
        }
      } catch (reason) {
        if (gate.isCurrent(token)) {
          setLogin(null); setError(reason.status === 409 ? '设置已变化 · 刷新' : '登录未完成 · 重试');
        }
      }
    }, Math.max(3, login.poll_after || 3) * 1000);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [api, login, scope]);
  async function run(operation) {
    if (busy) return;
    const token = gate.issue('write', scope); if (!token) return;
    const controller = new AbortController(); writer.current = controller;
    setBusy(true); setError('');
    try { await operation(controller.signal, token); }
    catch (reason) { if (gate.isCurrent(token)) setError(reason.status === 409 ? '设置已变化 · 刷新' : '操作未完成 · 重试'); }
    finally { if (gate.isCurrent(token)) { setBusy(false); setContent(''); } }
  }
  async function cancel(signal) {
    if (login?.login_id) await api.cancel(login.login_id, signal);
    setLogin(null);
  }
  async function save(platform, text, signal, token) {
    await cancel(signal);
    await api.save(platform, text, rows.find(row => row.platform === platform).revision, signal);
    if (gate.isCurrent(token)) setRefresh(old => old + 1);
  }
  return <div className="platform-sessions">
    {error && <p role="alert">{error} <button type="button" disabled={busy} onClick={() => { setError(''); setRefresh(old => old + 1); }}>刷新</button></p>}
    {!rows ? <span role="status">读取中</span> : rows.map(row => <section className="settings-item" key={row.platform}>
      <Row title={names[row.platform]} expanded={open === row.platform} onOpen={() => {
        if (busy) return;
        setContent(''); setOpen(old => old === row.platform ? null : row.platform);
      }} trailing={<><span className="ui-row-meta" title="本地保存状态；网站仍可能要求重新登录">{row.saved ? '已保存' : '未保存'}</span><StatusDot state={row.saved ? 'done' : 'unverified'}/></>}/>
      {open === row.platform && <div className="settings-expand">
        <div className="platform-session-actions">
          <button type="button" disabled={busy || !row.qr_available} title={row.qr_available ? '显示网站真实登录码；用对应 App 扫码确认' : '扫码环境未就绪，可先导入 Cookie'} onClick={() => run(async (signal, token) => {
            await cancel(signal);
            const result = await api.start(row.platform, signal);
            if (gate.isCurrent(token)) setLogin(result);
          })}>扫码登录</button>
          {row.saved && <button type="button" disabled={busy} onClick={() => run(async (signal, token) => {
            await cancel(signal); await api.remove(row.platform, row.revision, signal);
            if (gate.isCurrent(token)) setRefresh(old => old + 1);
          })}>删除登录</button>}
        </div>
        {login?.platform === row.platform && <div className="platform-session-qr">
          {['waiting', 'scanned'].includes(login.state) && <img src={login.qr_image} alt={`${names[row.platform]}登录二维码`} width="240" height="240"/>}
          <span role="status">{states[login.state] || '登录中'}</span>
          <button type="button" disabled={busy} onClick={() => run(signal => cancel(signal))}>关闭</button>
        </div>}
        <form onSubmit={event => { event.preventDefault(); const input = content; setContent(''); run((signal, token) => save(row.platform, input, signal, token)); }}>
          <label title="只发送到所选网站；加密保存，不显示已保存值">Cookie<input type="password" aria-label={`${names[row.platform]} Cookie`} value={content} autoComplete="off" spellCheck="false" disabled={busy} onChange={event => setContent(event.target.value)}/></label>
          <button type="submit" disabled={busy || !content.trim()}>导入</button>
          <label className="platform-session-file" title="cookies.txt 或浏览器导出的 Cookie JSON，最大 64KB">导入文件<input type="file" aria-label={`导入${names[row.platform]} Cookie文件`} accept=".txt,.json" disabled={busy} onChange={event => {
            const file = event.target.files?.[0]; event.target.value = ''; if (!file) return;
            if (file.size > 65536) { setError('文件过大 · 换文件'); return; }
            run(async (signal, token) => { const text = await file.text(); if (gate.isCurrent(token)) await save(row.platform, text, signal, token); });
          }}/></label>
        </form>
      </div>}
    </section>)}
  </div>;
}
