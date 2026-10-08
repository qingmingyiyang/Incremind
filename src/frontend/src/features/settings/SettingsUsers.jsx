import { useEffect, useState } from 'react';
import { Row, Switch } from '../../shared/ui';
import { formatLocalShortDate } from '../../shared/lib/time';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { selectUserSpace, userStorageKey } from '../../shared/api/deviceTransport';
import { usersApi } from './usersApi';
import { DevicePairing } from './DevicePairing';

export function SettingsUsers({ projectId }) {
  const [value, setValue] = useState(null), [audit, setAudit] = useState([]), [open, setOpen] = useState(null);
  const [showAudit, setShowAudit] = useState(false), [name, setName] = useState(''), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const [qr,setQr] = useState(null), [refresh,setRefresh] = useState(0);
  const gate = useRequestScope(userStorageKey(projectId)), scope = gate.scope;
  useEffect(() => {
    const token = gate.issue('load', scope);
    setValue(null); setAudit([]); setOpen(null); setQr(null); setError('');
    Promise.all([usersApi.list(), usersApi.audit()]).then(([users, history]) => {
      if (gate.isCurrent(token)) { setValue(users); setAudit(history.items || []); }
    }).catch(() => { if (gate.isCurrent(token)) setError('读取未完成 · 重试'); });
    return () => { gate.invalidate('load'); gate.invalidate('write'); };
  }, [scope,refresh]);
  async function run(operation) {
    if (busy) return;
    const token = gate.issue('write', scope); if (!token) return;
    setBusy(true); setError('');
    try { await operation(token); }
    catch (reason) { if (gate.isCurrent(token)) setError(reason.status === 409 ? '用户已变化 · 刷新' : '操作未完成 · 重试'); }
    finally { if (gate.isCurrent(token)) setBusy(false); }
  }
  const update = (user, changes) => run(async token => {
    const result = await usersApi.update(user, changes);
    if (gate.isCurrent(token)) { setValue(old => ({ ...old, items: old.items.map(row => row.user_id === user.user_id ? { ...row, ...result } : row) })); if (result.disabled_at) setQr(null); }
  });
  return <section className="settings-users">{error && <p role="alert">{error} <button type="button" onClick={()=>setRefresh(value=>value+1)}>刷新</button></p>}
    {value?.caller?.role === 'admin' && <>
      {(value.items || []).map(user => <div key={user.user_id}>
        <Row title={user.name} expanded={open === user.user_id} onOpen={() => setOpen(open === user.user_id ? null : user.user_id)}
          meta={user.disabled_at ? '已停用' : `${user.device_count || 0} · ${(Number(user.storage_bytes || 0) / 1048576).toFixed(1)} MB`}/>
        {open === user.user_id && <div className="settings-expand">
          <Row title="停用" readOnly trailing={<Switch label={`停用${user.name}`} checked={Boolean(user.disabled_at)} disabled={busy} onChange={disabled => update(user, { disabled })}/>}/>
          <QuotaForm user={user} disabled={busy} onSave={changes => update(user, changes)}/>
          <div className="settings-device-pair"><button type="button" aria-label={`给${user.name}添加设备`} disabled={busy || Boolean(user.disabled_at)} onClick={()=>run(async token=>{
            setQr(null); const issued=await usersApi.pair(user);
            if (gate.isCurrent(token)) setQr({...issued,user_id:user.user_id});
          })}>{qr?.user_id===user.user_id ? '重新生成' : '添加设备'}</button>
            <DevicePairing value={qr?.user_id===user.user_id ? qr : null}/></div>
          <button type="button" disabled={busy} onClick={() => selectUserSpace(user)} aria-label={`进入${user.name}的空间`}>进入空间</button>
        </div>}
      </div>)}
      <form onSubmit={event => { event.preventDefault(); run(async token => {
        const created = await usersApi.create(name.trim());
        if (gate.isCurrent(token)) { setValue(old => ({ ...old, items: [...old.items, created] })); setName(''); }
      }); }}><label>名称<input aria-label="新用户名称" maxLength={80} value={name} onChange={event => setName(event.target.value)} disabled={busy}/></label>
        <button type="submit" disabled={busy || !name.trim()}>新建用户</button></form>
    </>}
    <button type="button" className="settings-user-audit-toggle" aria-expanded={showAudit} onClick={() => setShowAudit(!showAudit)}>访问记录</button>
    {showAudit && <div className="settings-user-audit">{audit.filter(row => row.phase === 'intent').map(row => <div key={row.audit_id}>
      <time>{formatLocalShortDate(row.at)}</time><span>{row.method} {row.path}</span>
    </div>)}</div>}
  </section>;
}

function QuotaForm({ user, disabled, onSave }) {
  const [storage, setStorage] = useState(user.storage_limit_mb ?? ''), [minutes, setMinutes] = useState(user.job_minutes_per_day ?? '');
  return <form onSubmit={event => { event.preventDefault(); onSave({ storage_limit_mb: storage === '' ? null : Number(storage), job_minutes_per_day: minutes === '' ? null : Number(minutes) }); }}>
    <label>空间 MB<input aria-label={`${user.name}空间上限`} type="number" min="0" step="1" placeholder="—" value={storage} disabled={disabled} onChange={event => setStorage(event.target.value)}/></label>
    <label>分钟 / 天<input aria-label={`${user.name}后台时长上限`} type="number" min="0" step="1" placeholder="—" value={minutes} disabled={disabled} onChange={event => setMinutes(event.target.value)}/></label>
    <button type="submit" disabled={disabled}>保存</button>
  </form>;
}
