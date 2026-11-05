import { useEffect, useRef, useState } from 'react';
import { Row, Switch } from '../../shared/ui';
import { formatLocalShortDate } from '../../shared/lib/time';
import { formatBytes } from '../../shared/lib/formatBytes';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { readUserSpace, selectUserSpace, userStorageKey } from '../../shared/api/deviceTransport';
import { usersApi } from './usersApi';
import { DevicePairing } from './DevicePairing';
import { DeleteConfirmation } from './DeleteConfirmation';
import { LOGIN_NAME_PATTERN, validLogin } from './loginRules';
import { ServerModelProfiles, UserModelAllocation, UserModelBalance } from './ServerModelSettings';
import './SettingsSupport.css';

export function SettingsUsers({ projectId }) {
  const [value, setValue] = useState(null), [audit, setAudit] = useState([]), [open, setOpen] = useState(null);
  const [showAudit, setShowAudit] = useState(false), [name, setName] = useState(''), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const [qr,setQr] = useState(null), [refresh,setRefresh] = useState(0);
  const [loginName, setLoginName] = useState(''), [password, setPassword] = useState('');
  const [auditDetails, setAuditDetails] = useState({});
  const auditRequests = useRef(new Set());
  const gate = useRequestScope(userStorageKey(projectId)), scope = gate.scope;
  useEffect(() => {
    const token = gate.issue('load', scope);
    setValue(null); setAudit([]); setAuditDetails({}); setOpen(null); setQr(null); setError(''); setBusy(false); setName(''); setLoginName(''); setPassword(''); setShowAudit(false);
    Promise.all([usersApi.list(), usersApi.audit()]).then(([users, history]) => {
      if (gate.isCurrent(token)) { setValue(users); setAudit(history.entries || []); }
    }).catch(() => { if (gate.isCurrent(token)) setError('读取未完成 · 重试'); });
    return () => {
      gate.invalidate('load'); gate.invalidate('write');
      for (const channel of auditRequests.current) gate.invalidate(channel);
      auditRequests.current.clear();
    };
  }, [scope,refresh]);
  async function readAudit(row) {
    if (auditDetails[row.audit_id]?.loading) return;
    const channel = 'audit:' + row.audit_id;
    auditRequests.current.add(channel);
    const token = gate.issue(channel, scope);
    if (!token) return;
    setAuditDetails(old => ({ ...old, [row.audit_id]: { loading: true } }));
    try {
      const data = await usersApi.auditObjects(row.audit_id);
      if (data?.audit_id !== row.audit_id || !['committed', 'accepted', 'rejected', 'unknown'].includes(data.business_state)
          || !Array.isArray(data.objects) || data.objects.length > 1) throw new Error('audit_details_invalid');
      if (gate.isCurrent(token)) setAuditDetails(old => ({ ...old, [row.audit_id]: { data } }));
    } catch {
      if (gate.isCurrent(token)) setAuditDetails(old => ({ ...old, [row.audit_id]: { error: true } }));
    }
  }
  async function run(operation) {
    if (busy) return;
    const token = gate.issue('write', scope); if (!token) return;
    setBusy(true); setError('');
    try { return await operation(token); }
    catch (reason) { if (gate.isCurrent(token)) setError({ user_delete_busy: '任务仍在运行 · 停止后重试', user_delete_shared_owner: '仍拥有共享项目 · 转让后重试', user_delete_related_data: '仍有关联资料 · 处理后重试', user_delete_incomplete: '删除未完成 · 重试删除', user_delete_requires_disabled: '账号仍启用 · 先停用', user_delete_protected: '账号受保护 · 刷新', user_deletion_in_progress: '删除未完成 · 重试删除', user_delete_path_invalid: '空间目录异常 · 检查后重试' }[reason.code] || (reason.status === 409 ? '用户已变化 · 刷新' : '操作未完成 · 重试')); }
    finally { if (gate.isCurrent(token)) setBusy(false); }
  }
  const update = (user, changes) => run(async token => {
    const result = await usersApi.update(user, changes);
    if (gate.isCurrent(token)) { setValue(old => ({ ...old, items: old.items.map(row => row.user_id === user.user_id ? { ...row, ...result } : row) })); if (result.disabled_at) setQr(null); return result; }
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
  return <section className="settings-users settings-users-revised" aria-label="用户管理">{error && <p role="alert">{error} <button type="button" onClick={()=>setRefresh(value=>value+1)}>刷新</button></p>}
    {value?.caller?.role === 'admin' && <>
      <h2>用户 <span>{value.items?.length || 0}</span><button type="button" disabled={busy} aria-label="刷新用户管理" onClick={() => setRefresh(old => old + 1)}>刷新</button></h2>
      <div className="settings-card"><div className="settings-user-columns" aria-hidden="true"><span>姓名</span><span>角色 · 状态 · 设备 · 空间</span><span>操作</span></div>
      {(value.items || []).map(user => <div key={user.user_id}>
        <Row className="settings-user-row" dot={<span className="settings-user-avatar" aria-hidden="true">{Array.from(user.name || '')[0]}</span>} title={user.name} sub={user.login_name || undefined} expanded={open === user.user_id} onOpen={() => { setOpen(open === user.user_id ? null : user.user_id); setQr(null); }}
          meta={<span className="settings-user-facts"><span className="settings-support-tag">{user.role === 'admin' ? '管理员' : '用户'}</span><span>{user.deletion_pending ? '清理中' : user.disabled_at ? '已停用' : '启用'}</span><span>{user.device_count || 0} 台</span><span className="settings-user-storage">{Number.isFinite(user.storage_limit_mb) && user.storage_limit_mb > 0 && Number.isFinite(user.storage_bytes) && <meter min="0" max={user.storage_limit_mb * 1024 * 1024} value={Math.min(user.storage_bytes, user.storage_limit_mb * 1024 * 1024)} aria-label={`${user.name}存储用量`}/>}{formatBytes(user.storage_bytes)}{Number.isFinite(user.storage_limit_mb) && user.storage_limit_mb > 0 ? ` / ${formatBytes(user.storage_limit_mb * 1024 * 1024)}` : ''}</span></span>}
          trailing={<div className="settings-user-actions"><button type="button" disabled={busy || user.deletion_pending} onClick={() => selectUserSpace(user)} aria-label={`进入${user.name}的空间`}>进入空间</button>
            <button type="button" aria-label={`管理${user.name}`} aria-expanded={open === user.user_id} disabled={busy}
              onClick={() => { setOpen(open === user.user_id ? null : user.user_id); setQr(null); }}>管理</button></div>}/>
        {open === user.user_id && <div className="settings-expand">
          <LoginForm key={`${scope}:${user.user_id}:login`} user={user} disabled={busy || user.deletion_pending} onSave={(account, secret) => run(async token => {
            const result = await usersApi.login(user, account, secret);
            if (gate.isCurrent(token)) { setValue(old => ({ ...old, items: old.items.map(row => row.user_id === user.user_id ? { ...row, ...result } : row) })); return result; }
          })}/>
          <Row title="停用" readOnly trailing={<Switch label={`停用${user.name}`} title="停用账号，保留空间资料和访问记录" checked={Boolean(user.disabled_at)} disabled={busy || user.deletion_pending || user.user_id === value.caller.user_id || user.role === 'admin'} onChange={disabled => update(user, { disabled })}/>}/>
          <QuotaForm key={`${scope}:${user.user_id}`} user={user} disabled={busy || user.deletion_pending} onSave={changes => update(user, changes)}/>
          {!user.deletion_pending && <><UserModelAllocation key={`${scope.key}:${user.user_id}:allocation`} user={user} scopeKey={scope.key}/><UserModelBalance key={`${scope.key}:${user.user_id}:balance`} user={user} admin scopeKey={scope.key}/></>}
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
        if (!name.trim() || !validLogin(loginName, password)) return;
        const created = await usersApi.create(name.trim(), loginName.trim(), password);
        if (gate.isCurrent(token)) { setValue(old => ({ ...old, items: [...old.items, created] })); setName(''); setLoginName(''); setPassword(''); }
      }); }}><label>名称<input aria-label="新用户名称" maxLength={80} value={name} onChange={event => setName(event.target.value)} disabled={busy}/></label>
        <label>账号<input aria-label="新用户账号" autoComplete="off" autoCapitalize="none" minLength={3} maxLength={64} pattern={LOGIN_NAME_PATTERN} value={loginName} onChange={event => setLoginName(event.target.value)} disabled={busy}/></label>
        <label>初始密码<input aria-label="新用户初始密码" type="password" autoComplete="new-password" minLength={15} maxLength={512} value={password} onChange={event => setPassword(event.target.value)} disabled={busy}/></label>
        <span className="settings-login-hint">账号 3 至 64 位，使用英文字母、数字或 . _ -；密码 15 至 256 位</span>
        <button type="submit" disabled={busy || !name.trim() || !validLogin(loginName, password)}>新建用户</button></form></div>
    </>}
    {value?.caller?.role === 'admin' && <ServerModelProfiles key={scope.key} scopeKey={scope.key}/>}
    {value?.caller && value.caller.role !== 'admin' && <UserModelBalance key={scope.key} user={value.caller} admin={false} scopeKey={scope.key}/>}
    {!value && !error && <span role="status">读取中</span>}
    <button type="button" className="settings-user-audit-toggle" aria-expanded={showAudit} onClick={() => setShowAudit(!showAudit)}>访问记录</button>
    {showAudit && <div className="settings-user-audit">{audit.map(row => {
      const detail = auditDetails[row.audit_id], data = detail?.data;
      return <div key={row.audit_id}>
        <time>{formatLocalShortDate(row.at)}</time><span>{row.method} {row.path}</span>
        <span>{row.outcomes.length ? row.outcomes.map(outcome => `HTTP ${outcome.status}`).join(' · ') : '请求结果待核对'}</span>
        <span>{({ committed: '业务已提交', accepted: '业务已受理', rejected: '业务已拒绝', unknown: '业务结果未知' })[data?.business_state || 'unknown']}</span>
        <button type="button" disabled={detail?.loading} aria-expanded={Boolean(data)} onClick={() => readAudit(row)}>
          {detail?.loading ? '读取中' : detail?.error ? '重新读取对象' : '查看对象'}</button>
        {detail?.error && <span role="alert">对象未读取</span>}
        {data && <section aria-label="访问对象">{data.objects.length === 0 ? <span>对象未知</span> : data.objects.map(object => <p key={object.object_id}>
          <span>{object.state === 'available' ? object.current_title || object.current_label : '对象当前不可核对'}</span>
          <span> · #{object.project_id} · 记录修订 {object.recorded_revision}</span>
          {object.state === 'available' && <span> · {object.current_title ? '当前标题' : '当前对象'} · 修订 {object.current_revision}</span>}
        </p>)}</section>}
      </div>;
    })}</div>}
  </section>;
}

function LoginForm({ user, disabled, onSave }) {
  const [name, setName] = useState(user.login_name || ''), [password, setPassword] = useState(''), [saved, setSaved] = useState(false);
  const edited = useRef(0);
  useEffect(() => { if (!edited.current) setName(user.login_name || ''); }, [user.login_name]);
  return <form className="settings-user-login" onSubmit={async event => {
    event.preventDefault(); if (disabled || !validLogin(name, password)) return;
    const revision = edited.current;
    const result = await onSave(name.trim(), password);
    if (result && edited.current === revision) { edited.current = 0; setName(result.login_name || ''); setPassword(''); setSaved(true); }
  }}>
    <p className="settings-login-hint">{user.login_configured === true ? `登录账号 · ${user.login_name || ''}` : '尚未设置登录账号'}</p>
    <label>账号<input aria-label={`${user.name}登录账号`} autoComplete="off" autoCapitalize="none" minLength={3} maxLength={64} pattern={LOGIN_NAME_PATTERN} disabled={disabled} value={name} onChange={event => { edited.current += 1; setSaved(false); setName(event.target.value); }}/></label>
    <label>新密码<input aria-label={`${user.name}新密码`} type="password" autoComplete="new-password" minLength={15} maxLength={512} disabled={disabled} value={password} onChange={event => { edited.current += 1; setSaved(false); setPassword(event.target.value); }}/></label>
    <span className="settings-login-hint">密码 15 至 256 位。保存后，该账号通过密码登录的设备需要重新登录。</span>
    <button type="submit" disabled={disabled || !validLogin(name, password)}>保存登录账号</button>
    {saved && <span role="status">已保存</span>}
  </form>;
}

function QuotaForm({ user, disabled, onSave }) {
  const [storage, setStorage] = useState(user.storage_limit_mb ?? ''), [minutes, setMinutes] = useState(user.job_minutes_per_day ?? '');
  const edited = useRef({ storage: 0, minutes: 0 });
  useEffect(() => { if (!edited.current.storage) setStorage(user.storage_limit_mb ?? ''); }, [user.storage_limit_mb, user.revision]);
  useEffect(() => { if (!edited.current.minutes) setMinutes(user.job_minutes_per_day ?? ''); }, [user.job_minutes_per_day, user.revision]);
  function adjust(field, amount) {
    edited.current[field] += 1;
    if (field === 'storage') setStorage(current => String(Math.max(0, Number(current || 0) + amount)));
    else setMinutes(current => String(Math.max(0, Number(current || 0) + amount)));
  }
  return <form className="settings-user-quota" onSubmit={async event => {
    event.preventDefault();
    const edit = { ...edited.current };
    const saved = await onSave({ storage_limit_mb: storage === '' ? null : Number(storage), job_minutes_per_day: minutes === '' ? null : Number(minutes) });
    if (!saved) return;
    // 仅接收本次保存成功且期间未继续编辑的字段。
    if (edited.current.storage === edit.storage) { edited.current.storage = 0; setStorage(saved.storage_limit_mb ?? ''); }
    if (edited.current.minutes === edit.minutes) { edited.current.minutes = 0; setMinutes(saved.job_minutes_per_day ?? ''); }
  }}>
    <label>存储 · MB<span className="settings-quota-step"><button type="button" disabled={disabled} aria-label={`减少${user.name}存储上限`} onClick={() => adjust('storage', -1024)}>−</button><input aria-label={`${user.name}空间上限`} type="number" min="0" step="1" placeholder="—" value={storage} disabled={disabled} onChange={event => { edited.current.storage += 1; setStorage(event.target.value); }}/><button type="button" disabled={disabled} aria-label={`增加${user.name}存储上限`} onClick={() => adjust('storage', 1024)}>＋</button></span></label>
    <label>后台 · 分钟 / 天<span className="settings-quota-step"><button type="button" disabled={disabled} aria-label={`减少${user.name}后台时长上限`} onClick={() => adjust('minutes', -30)}>−</button><input aria-label={`${user.name}后台时长上限`} type="number" min="0" step="1" placeholder="—" value={minutes} disabled={disabled} onChange={event => { edited.current.minutes += 1; setMinutes(event.target.value); }}/><button type="button" disabled={disabled} aria-label={`增加${user.name}后台时长上限`} onClick={() => adjust('minutes', 30)}>＋</button></span></label>
    <button type="submit" disabled={disabled}>保存</button>
  </form>;
}
