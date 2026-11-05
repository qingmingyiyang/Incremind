import { useEffect, useRef, useState } from 'react';
import { Row, StatusDot, Icon } from '../../shared/ui';
import { userStorageKey } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { platformSessionApi } from './platformSessionApi';
import { beginCredentialDraft, completeCredentialDraft, editCredentialDraft, receiveCredentialDraft, reviewCredentialDraft, submitCredentialDraft } from './credentialDraft';
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
  const [error, setError] = useState(''), [reading, setReading] = useState(false), [readError, setReadError] = useState(false);
  const [busy, setBusy] = useState(false), [login, setLogin] = useState(null), [refresh, setRefresh] = useState(0);
  const [method, setMethod] = useState('import'), [session, setSession] = useState(0);
  const selected = rows?.find(row => row.platform === open), draftScope = `${space}:${open || ''}:${session}`, revision = selected?.revision ?? 0;
  const [draft, setDraft] = useState(() => beginCredentialDraft(draftScope, revision, { content: '' }));
  const currentDraft = receiveCredentialDraft(draft, draftScope, revision, { content: '' }), content = currentDraft.fields.content;
  const writer = useRef(null);
  const current = token => userStorageKey('platform-sessions') === space && gate.isCurrent(token);
  useEffect(() => { setDraft(old => receiveCredentialDraft(old, draftScope, revision, { content: '' })); }, [draftScope, revision]);
  useEffect(() => {
    const controller = new AbortController(), token = gate.issue('load', scope);
    setReading(true); setReadError(false);
    api.load(controller.signal).then(value => {
      if (!Array.isArray(value?.items) || value.items.some(row => !Object.hasOwn(names, row.platform) || !Number.isInteger(row.revision) || row.revision < 0 || typeof row.saved !== 'boolean')) throw new Error('platform_status_invalid');
      if (current(token)) setRows(value.items);
    })
      .catch(() => { if (current(token)) { setError('读取未完成 · 刷新'); setReadError(true); } })
      .finally(() => { if (current(token)) setReading(false); });
    return () => { controller.abort(); writer.current?.abort(); };
  }, [api, scope, refresh]);
  useEffect(() => {
    if (!login || !['waiting', 'scanned'].includes(login.state)) return undefined;
    const controller = new AbortController(), token = gate.issue('poll', scope);
    const timer = setTimeout(async () => {
      try {
        if (!current(token)) return;
        const result = await api.poll(login.login_id, controller.signal);
        if (current(token)) {
          setLogin(old => ({ ...old, ...result }));
          if (result.state === 'saved') {
            setRefresh(old => old + 1);
            setDraft(old => old.scope === login.draftScope && old.version === login.draftVersion
              ? beginCredentialDraft(old.scope, old.currentIdentity, { content: '' }) : old);
          }
        }
      } catch (reason) {
        if (current(token)) {
          setLogin(null); setError(reason.status === 409 ? '设置已变化 · 刷新' : '登录未完成 · 重试');
        }
      }
    }, Math.max(3, login.poll_after || 3) * 1000);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [api, login, scope]);
  async function run(operation) {
    if (busy) return;
    const token = gate.issue('write', scope); if (!token || !current(token)) return;
    const controller = new AbortController(); writer.current = controller;
    setBusy(true); setError('');
    try { await operation(controller.signal, token); }
    catch (reason) { if (current(token)) { setError(reason.status === 409 ? '设置已变化 · 核对后重试' : '操作未完成 · 输入已保留'); setRefresh(old => old + 1); } }
    finally { if (current(token)) setBusy(false); }
  }
  async function cancel(signal, token) {
    if (!current(token)) return;
    if (login?.login_id) await api.cancel(login.login_id, signal);
    if (current(token)) setLogin(null);
  }
  async function save(platform, submitted, signal, token) {
    await cancel(signal, token);
    if (!current(token)) return;
    const saved = await api.save(platform, submitted.fields.content, submitted.identity, signal);
    if (saved?.platform !== platform || !Number.isInteger(saved.revision) || saved.revision <= submitted.identity || saved.saved !== true) throw new Error('platform_save_result_invalid');
    if (current(token)) {
      setDraft(old => completeCredentialDraft(old, submitted, saved.revision, { content: '' }));
      setRefresh(old => old + 1);
    }
  }
  function toggle(platform) {
    if (busy) return;
    const next = open === platform ? null : platform;
    setDraft(beginCredentialDraft(`${space}:${next || ''}:${session + 1}`, rows.find(row => row.platform === next)?.revision ?? 0, { content: '' }));
    setSession(old => old + 1); setOpen(next);
  }
  return <div className="platform-sessions settings-card">
    {error && <p role="alert">{error} <button type="button" disabled={busy} onClick={() => { setError(''); setRefresh(old => old + 1); }}>刷新</button></p>}
    {!rows ? <span role="status">读取中</span> : rows.map(row => <section className="settings-item" key={row.platform}>
      <Row dot={<span className="settings-glyph"><Icon name="link" size={18}/></span>} title={<span>{names[row.platform]}</span>} sub={<span aria-hidden="true" className="settings-model-summary"><StatusDot state={row.saved ? 'done' : 'unverified'}/>{row.saved ? '已保存' : '未保存'}</span>} expanded={open === row.platform} onOpen={() => toggle(row.platform)} trailing={<><span className="settings-accessible-summary" role="status" aria-label={`${names[row.platform]} · ${row.saved ? '已保存' : '未保存'}`}/><button type="button" className="settings-chevron" aria-label="展开" aria-expanded={open === row.platform} title="本地保存状态；网站仍可能要求重新登录" onClick={() => toggle(row.platform)}><Icon name="chevron-down" size={16}/></button></>}/>
      {open === row.platform && <div className="settings-expand">
        <div className="settings-modes" role="group" aria-label="登录方式">
          <button type="button" aria-pressed={method === 'qr'} disabled={busy || !row.qr_available} title={row.qr_available ? '显示网站真实登录码；用对应 App 扫码确认' : '扫码环境未就绪，可先导入 Cookie'} onClick={() => { setMethod('qr'); run(async (signal, token) => {
            await cancel(signal, token);
            if (!current(token)) return;
            const result = await api.start(row.platform, signal);
            if (current(token)) setLogin({ ...result, draftScope: currentDraft.scope, draftVersion: currentDraft.version });
          }); }}>扫码登录</button>
          <button type="button" aria-label="Cookie导入方式" aria-pressed={method === 'import'} disabled={busy} onClick={() => setMethod('import')}>导入</button>
        </div><div className="platform-session-actions">{row.saved && <button type="button" disabled={busy} onClick={() => run(async (signal, token) => {
            await cancel(signal, token); if (!current(token)) return;
            await api.remove(row.platform, row.revision, signal);
            if (current(token)) setRefresh(old => old + 1);
          })}>删除登录</button>}
        </div>
        {method === 'qr' && login?.platform === row.platform && <div className="platform-session-qr">
          {['waiting', 'scanned'].includes(login.state) && <img src={login.qr_image} alt={`${names[row.platform]}登录二维码`} width="240" height="240"/>}
          <span role="status">{states[login.state] || '登录中'}</span>
          <button type="button" disabled={busy} onClick={() => run((signal, token) => cancel(signal, token))}>关闭</button>
        </div>}
        {method === 'import' && <form onSubmit={event => { event.preventDefault(); if (reading || readError) return; run((signal, token) => save(row.platform, submitCredentialDraft(currentDraft), signal, token)); }}>
          <label title="只发送到所选网站；加密保存，不显示已保存值">Cookie<input type="password" aria-label={`${names[row.platform]} Cookie`} value={content} autoComplete="off" spellCheck="false" disabled={busy} onChange={event => setDraft(old => editCredentialDraft(receiveCredentialDraft(old, draftScope, revision, { content: '' }), 'content', event.target.value))}/></label>
          <button type="submit" disabled={busy || reading || readError || currentDraft.stale || !content.trim()}>导入</button>
          <label className="platform-session-file" title="cookies.txt 或浏览器导出的 Cookie JSON，最大 64KB">导入文件<input type="file" aria-label={`导入${names[row.platform]} Cookie文件`} accept=".txt,.json" disabled={busy || reading || readError} onChange={event => {
            const file = event.target.files?.[0]; event.target.value = ''; if (!file) return;
            if (file.size > 65536) { setError('文件过大 · 换文件'); return; }
            run(async (signal, token) => {
              const text = await file.text(); if (!current(token)) return;
              const next = editCredentialDraft(currentDraft, 'content', text); setDraft(next);
              await save(row.platform, submitCredentialDraft(next), signal, token);
            });
          }}/></label>
          {currentDraft.stale && <section className="settings-provider-revision" aria-label={`${names[row.platform]}登录配置变更`}><p role="status">登录设置已变化 · 核对后导入</p>
            <Row readOnly title="当前登录" meta={row.saved ? '已保存' : '未保存'}/>
            <button type="button" disabled={busy || reading || readError} onClick={() => setDraft(old => reviewCredentialDraft(receiveCredentialDraft(old, draftScope, revision, { content: '' })))}>重新核对</button></section>}
        </form>}
      </div>}
    </section>)}
  </div>;
}
