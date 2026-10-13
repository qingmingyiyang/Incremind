import { useEffect, useState } from 'react';
import { Row, Switch } from '../../shared/ui';
import { formatLocalShortDate } from '../../shared/lib/time';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { readUserSpace, selectUserSpace, userStorageKey } from '../../shared/api/deviceTransport';
import { usersApi } from './usersApi';
import { DevicePairing } from './DevicePairing';
import { DeleteConfirmation } from './DeleteConfirmation';

export function SettingsUsers({ projectId }) {
  const [value, setValue] = useState(null), [audit, setAudit] = useState([]), [open, setOpen] = useState(null);
  const [showAudit, setShowAudit] = useState(false), [name, setName] = useState(''), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const [qr,setQr] = useState(null), [refresh,setRefresh] = useState(0);
  const gate = useRequestScope(userStorageKey(projectId)), scope = gate.scope;
  useEffect(() => {
    const token = gate.issue('load', scope);
    setValue(null); setAudit([]); setOpen(null); setQr(null); setError(''); setBusy(false); setName(''); setShowAudit(false);
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
    catch (reason) { if (gate.isCurrent(token)) setError({ user_delete_busy: '任务仍在运行 · 停止后重试', user_delete_shared_owner: '仍拥有共享项目 · 转让后重试', user_delete_related_data: '仍有关联资料 · 处理后重试', user_delete_incomplete: '删除未完成 · 重试删除', user_delete_requires_disabled: '账号仍启用 · 先停用', user_delete_protected: '账号受保护 · 刷新', user_deletion_in_progress: '删除未完成 · 重试删除', user_delete_path_invalid: '空间目录异常 · 检查后重试' }[reason.code] || (reason.status === 409 ? '用户已变化 · 刷新' : '操作未完成 · 重试')); }
    finally { if (gate.isCurrent(token)) setBusy(false); }
  }
  const update = (user, changes) => run(async token => {
    const result = await usersApi.update(user, changes);
    if (gate.isCurrent(token)) { setValue(old => ({ ...old, items: old.items.map(row => row.user_id === user.user_id ? { ...row, ...result } : row) })); if (result.disabled_at) setQr(null); }
  });
  const remove = user => run(async token => {
    try {
      const result = await usersApi.remove(user);
      if (result?.deleted !== true || result.user_id !== user.user_id) throw new Error('user_delete_unconfirmed');
      if (gate.isCurrent(token)) {
        if (readUserSpace()?.user_id === user.user_id) {
          selectUserSpace(null);
          history.replaceState(null, '', location.pathname + '#view=settings&project_id=default&section=devices');
          window.dispatchEvent(new Event('hashchange'));
          return;
        }
        setValue(old => ({ ...old, items: old.items.filter(row => row.user_id !== user.user_id) })); setOpen(null); setQr(null);
      }
    } catch (reason) {
      // 删除可能已写入清理标记，先撤下旧列表，再读实际状态；读失败时只保留刷新入口。
      if (!gate.isCurrent(token)) throw reason;
      setValue(null); setOpen(null); setQr(null);
      try { const latest = await usersApi.list(); if (gate.isCurrent(token)) setValue(latest); }
      catch { /* 原删除错误仍交给统一错误行，不能恢复失效的列表。 */ }
      throw reason;
    }
  });
  return <section className="settings-users" aria-label="用户管理">{error && <p role="alert">{error} <button type="button" onClick={()=>setRefresh(value=>value+1)}>刷新</button></p>}
    {value?.caller?.role === 'admin' && <>
      <h2>用户管理 <span>{value.items?.length || 0}</span><button type="button" disabled={busy} aria-label="刷新用户管理" onClick={() => setRefresh(old => old + 1)}>刷新</button></h2>
      <div className="settings-user-columns" aria-hidden="true"><span>姓名</span><span>角色 · 状态 · 设备 · 空间</span><span>操作</span></div>
      {(value.items || []).map(user => <div key={user.user_id}>
        <Row className="settings-user-row" title={user.name} expanded={open === user.user_id} onOpen={() => { setOpen(open === user.user_id ? null : user.user_id); setQr(null); }}
          meta={<span className="settings-user-facts"><span>{user.role === 'admin' ? '管理员' : '用户'}</span><span>{user.deletion_pending ? '清理中' : user.disabled_at ? '已停用' : '启用'}</span><span>{user.device_count || 0} 台</span><span>{(Number(user.storage_bytes || 0) / 1048576).toFixed(1)} MB</span></span>}
          trailing={<div className="settings-user-actions"><button type="button" disabled={busy || user.deletion_pending} onClick={() => selectUserSpace(user)} aria-label={`进入${user.name}的空间`}>进入空间</button>
            <button type="button" aria-label={`管理${user.name}`} aria-expanded={open === user.user_id} disabled={busy}
              onClick={() => { setOpen(open === user.user_id ? null : user.user_id); setQr(null); }}>管理</button></div>}/>
        {open === user.user_id && <div className="settings-expand">
          <Row title="停用" readOnly trailing={<Switch label={`停用${user.name}`} title="停用账号，保留空间资料和访问记录" checked={Boolean(user.disabled_at)} disabled={busy || user.deletion_pending || user.user_id === value.caller.user_id || user.role === 'admin'} onChange={disabled => update(user, { disabled })}/>}/>
          <QuotaForm key={`${user.user_id}:${user.revision}`} user={user} disabled={busy || user.deletion_pending} onSave={changes => update(user, changes)}/>
          <div className="settings-device-pair"><button type="button" aria-label={`给${user.name}添加设备`} disabled={busy || Boolean(user.disabled_at)} onClick={()=>run(async token=>{
            setQr(null); const issued=await usersApi.pair(user);
            if (gate.isCurrent(token)) setQr({...issued,user_id:user.user_id});
          })}>{qr?.user_id===user.user_id ? '重新生成' : '添加设备'}</button>
            <DevicePairing value={qr?.user_id===user.user_id ? qr : null}/></div>
          {user.disabled_at && user.role === 'user' && user.user_id !== value.caller.user_id && <DeleteConfirmation key={`${user.user_id}:${user.revision}`} name={user.name}
            label={`删除${user.name}的账号和空间`} consequence={`将永久删除 ${user.name} 的账号、空间资料、设备和访问记录。`} disabled={busy}
            onConfirm={() => remove(user)}/>}
        </div>}
      </div>)}
      <form className="settings-user-create" onSubmit={event => { event.preventDefault(); run(async token => {
        const created = await usersApi.create(name.trim());
        if (gate.isCurrent(token)) { setValue(old => ({ ...old, items: [...old.items, created] })); setName(''); }
      }); }}><label>名称<input aria-label="新用户名称" maxLength={80} value={name} onChange={event => setName(event.target.value)} disabled={busy}/></label>
        <button type="submit" disabled={busy || !name.trim()}>新建用户</button></form>
    </>}
    {!value && !error && <span role="status">读取中</span>}
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
