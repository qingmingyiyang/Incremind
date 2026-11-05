import { useEffect, useRef, useState } from 'react';
import { Icon, StatusDot } from '../../shared/ui';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { userStorageKey, deviceIdentity, isDeviceIdentity } from '../../shared/api/deviceTransport';
import { readStoredJson, writeStoredJson, removeStoredItem } from '../../shared/lib/browserStorage';

function localInput(value) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return '';
  const part = number => String(number).padStart(2, '0');
  return `${date.getFullYear()}-${part(date.getMonth()+1)}-${part(date.getDate())}T${part(date.getHours())}:${part(date.getMinutes())}`;
}
function reminderTime(value) {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat('zh-CN', {month:'numeric',day:'numeric',weekday:'short',hour:'2-digit',minute:'2-digit',hour12:false}).format(date) : '—';
}
const errors = {
  reminder_time_required:'补充明确时间，或输入明天、周五等日期', invalid_reminder_at:'选择未来时间',
  reminder_revision_conflict:'提醒已变化 · 刷新后重新选择', reminder_binding_changed:'提醒已变化 · 刷新后重新选择',
  reminder_operation_conflict:'提交记录已变化 · 重新回读', reminder_operation_in_progress:'这条提醒还有未完成提交 · 先恢复原提交',
  reminder_source_not_retired:'原提醒仍可能送达 · 重新查看原提醒', reminder_operation_incomplete:'操作尚未完成 · 回读后继续',
  reminder_replacement_time_expired:'原定时间已过去 · 放弃本次后重新设置',
};
const explain = error => errors[error?.code] || '请求未完成 · 回读后重试';
const rowKey = item => `${item.source}:${item.id}`;
function readPending(slot, projectId) {
  const stored = readStoredJson(slot);
  if (stored.invalid) return {invalid:true};
  const value = stored.value;
  if (!value) return null;
  return typeof value.id === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(value.id)
    && value.body?.project_id === projectId && typeof value.body.action === 'string' ? value : {invalid:true};
}

export function CompanionReminders({ api, identity, projectId='default', focusedReminder, onNavigate }) {
  const slot = userStorageKey(`companion-reminder-operation:${projectId}`);
  const gate = useRequestScope(`${identity}:${projectId}`), scope = gate.scope;
  const [items,setItems] = useState([]), [loaded,setLoaded] = useState(false), [busy,setBusy] = useState(false);
  const [loadError,setLoadError] = useState(''), [error,setError] = useState(''), [message,setMessage] = useState(''), [attempt,setAttempt] = useState(0);
  const [title,setTitle] = useState(''), [at,setAt] = useState(''), [source,setSource] = useState(null), [expanded,setExpanded] = useState(null);
  const [pending,setPending] = useState(() => readPending(slot,projectId)), [operation,setOperation] = useState(null);
  const pendingRef = useRef(pending), writing = useRef(false), input = useRef(null);
  useEffect(() => { if(focusedReminder) setExpanded(`v2:${focusedReminder}`); },[focusedReminder]);
  const refresh = () => { gate.invalidate('load'); setAttempt(value => value+1); };
  useEffect(() => {
    const controller = new AbortController(), token = gate.issue('load',scope);
    api.listReminderCenter({projectId,signal:controller.signal}).then(value => {
      if (gate.isCurrent(token)) { setItems(value.items); setLoaded(true); setLoadError(''); }
    }).catch(reason => { if (gate.isCurrent(token)) { setLoaded(false); setLoadError(explain(reason)); } });
    return () => controller.abort();
  }, [api,projectId,scope,attempt]);
  function current(record,token,captured) {
    return gate.isCurrent(token) && isDeviceIdentity(captured) && pendingRef.current?.id === record.id;
  }
  function consume(value,record,token,captured) {
    if (!current(record,token,captured)) return;
    if (value?.id !== record.id || !['prepared','blocked','completed','abandoned'].includes(value.state)) {
      setError('结果尚未确认 · 回读本次提交'); return;
    }
    setOperation(value);
    if (value.state === 'completed' || value.state === 'abandoned') {
      if (readStoredJson(slot).value?.id === record.id) removeStoredItem(slot);
      pendingRef.current = null; setPending(null); setOperation(null);
      setTitle(''); setAt(''); setSource(null); setError('');
      setMessage(value.state === 'completed' ? record.body.source === 'legacy' && record.body.action === 'complete' ? '已完成本次' : ({create:'已创建',reschedule:'已改期',complete:'已完成',cancel:'已取消'})[record.body.action]
        : value.effects?.source_retired ? '已放弃后续操作 · 原提醒已取消' : '已放弃本次提交');
      refresh(); window.dispatchEvent(new Event('chriptmas-reminders-changed'));
    } else setError(value.error ? errors[value.error] || '操作尚未完成 · 查看当前状态' : '结果尚未确认 · 回读或继续本次提交');
  }
  async function readOperation(record=pendingRef.current) {
    if (!record || record.invalid) return;
    const token = gate.issue('operation',scope), captured = deviceIdentity();
    if (!token) return;
    try { consume(await api.readReminderOperation({id:record.id,projectId}),record,token,captured); }
    catch (reason) { if (current(record,token,captured)) {
      if (reason.status === 404) setOperation({id:record.id,state:'not_accepted',revision:0});
      setError(reason.status === 404 ? '尚未接纳本次提交 · 可以继续或放弃' : explain(reason));
    } }
  }
  useEffect(() => { if (pending && !pending.invalid && !writing.current) readOperation(pending); }, [scope,pending?.id]);
  async function send(record,mode='apply') {
    if (writing.current) return;
    const token = gate.issue('operation',scope), captured = deviceIdentity();
    if (!token) return;
    writing.current=true; setBusy(true); setError(''); setMessage('');
    try {
      let value;
      if (mode === 'abandon') {
        let actual;
        try { actual = await api.readReminderOperation({id:record.id,projectId}); }
        catch (reason) { if (reason.status !== 404) throw reason; actual={revision:0}; }
        if (!current(record,token,captured)) return;
        value = await api.abandonReminderOperation({id:record.id,body:{project_id:projectId,request:record.body,expected_revision:actual.revision}});
      } else if (mode === 'resume') {
        value = await api.resumeReminderOperation({id:record.id,body:{project_id:projectId,expected_revision:operation.revision,source_revision:operation.observed.source_revision,occurrences:operation.observed.occurrences}});
      } else value = await api.applyReminderOperation({id:record.id,body:record.body});
      consume(value,record,token,captured);
    } catch (reason) { if (current(record,token,captured)) {
      if (mode === 'apply' && record.body.action === 'create' && reason.status === 400) {
        if (readStoredJson(slot).value?.id === record.id) removeStoredItem(slot);
        pendingRef.current=null; setPending(null); setOperation(null);
      }
      setError(explain(reason));
    } }
    finally { writing.current=false; if (gate.isCurrent(token)) setBusy(false); }
  }
  function begin(body) {
    if (writing.current || pendingRef.current || !loaded) return;
    const record = {id:crypto.randomUUID(),body};
    if (!writeStoredJson(slot,record)) { setError('无法保存提交身份 · 检查浏览器存储'); return; }
    pendingRef.current=record; setPending(record); setOperation(null); send(record);
  }
  function action(item,name) {
    begin({project_id:projectId,action:name,source:item.source,id:item.id,
      expected_revision:item.revision,binding_revision:item.binding_revision,
      ...(name === 'complete' && item.source === 'legacy' ? {occurrence:item.complete_occurrence} : {})});
  }
  function arrange(item) {
    setSource(item); setTitle(item.title); setAt(localInput(item.next_at || item.scheduled_at)); setError(''); input.current?.focus();
  }
  async function restore(item) {
    const saved = item.pending_operation;
    if (saved.project_id !== projectId) { onNavigate?.('companion',{project_id:saved.project_id,tab:'schedule'}); return; }
    if (pendingRef.current || writing.current) return;
    const token=gate.issue('operation',scope), captured=deviceIdentity();
    try {
      const value=await api.readReminderOperation({id:saved.id,projectId});
      if (!gate.isCurrent(token) || !isDeviceIdentity(captured)) return;
      const record={id:saved.id,body:value.request};
      if (!writeStoredJson(slot,record)) { setError('无法保存提交身份 · 检查浏览器存储'); return; }
      pendingRef.current=record; setPending(record); consume(value,record,token,captured);
    } catch (reason) { if (gate.isCurrent(token)) setError(explain(reason)); }
  }
  function submit(event) {
    event.preventDefault();
    if (!title.trim() || source && !at) return;
    const date = at ? new Date(at) : null;
    if (date && (!Number.isFinite(date.getTime()) || date.getTime() <= Date.now())) { setError('选择未来时间'); return; }
    begin(source ? {project_id:projectId,action:'reschedule',source:source.source,id:source.id,
      expected_revision:source.revision,binding_revision:source.binding_revision,at:date.toISOString()}
      : {project_id:projectId,action:'create',text:title.trim(),...(date ? {at:date.toISOString()} : {})});
  }
  const disabled = busy || !loaded || Boolean(pending);
  const resume = pending && operation?.error !== 'reminder_replacement_time_expired' && operation?.observed?.source_revision;
  return <section className="companion-page-reminders" aria-label="提醒" aria-busy={busy}>
    <h3 className="companion-page-section">提醒 <span>{loaded ? items.filter(item => ['active','pending'].includes(item.state)).length : '—'}</span><button type="button" aria-label="刷新提醒" disabled={busy} onClick={refresh}><Icon name="review-reask" size={15}/></button></h3>
    {items.map(item => <div key={rowKey(item)} className="companion-page-reminder-entry">
      <div className="companion-page-reminder"><StatusDot state={['done','completed','acknowledged'].includes(item.state) ? 'done' : ['deleted','cancelled'].includes(item.state) ? 'forgotten' : 'pending'}/>
        <button type="button" className="companion-page-reminder-title" aria-expanded={expanded===rowKey(item)} onClick={() => setExpanded(expanded===rowKey(item)?null:rowKey(item))}>{item.title}</button>
        {item.actions.includes('reschedule') ? <button type="button" className="companion-page-time" aria-label={`修改提醒时间 ${item.title}`} disabled={disabled} onClick={() => arrange(item)}><Icon name="calendar" size={14}/><time dateTime={item.next_at || item.scheduled_at}>{reminderTime(item.next_at || item.scheduled_at)}</time></button> : <time>{reminderTime(item.scheduled_at)}</time>}
        {item.actions.includes('complete') && <button type="button" className="companion-page-icon" disabled={disabled} aria-label={item.source==='legacy'?'完成本次提醒':'完成提醒'} title={item.source==='legacy'?'完成本次':'完成'} onClick={() => action(item,'complete')}>✓</button>}
        {item.actions.includes('cancel') && <button type="button" className="companion-page-icon" disabled={disabled} aria-label={`取消提醒 ${item.title}`} onClick={() => action(item,'cancel')}><Icon name="close" size={15}/></button>}
        {item.pending_operation && !pending && <button type="button" className="companion-page-reminder-restore" disabled={busy} onClick={() => restore(item)}>恢复未完成操作</button>}
      </div>
      {expanded===rowKey(item) && <div className="companion-page-reminder-detail"><span>{item.project_id ? `#${item.project_id}${item.scene?'/'+item.scene:''}` : '全部项目 · 原手动提醒'}</span><span>{item.private?'私密 · 不送达提醒':item.recurrence==='daily'?`每天 · 剩余${item.remaining_count}次`:'单次'}</span><span>{({active:'待提醒',pending:'待提醒',done:'已完成',completed:'已完成',acknowledged:'已确认',deleted:'已取消',cancelled:'已取消'})[item.state] || item.state}</span>{item.turn_id && onNavigate && <button type="button" onClick={() => onNavigate('workbench',{project_id:item.project_id,turn_id:item.turn_id})}>原对话</button>}</div>}
    </div>)}
    <form className="companion-page-reminder-form" onSubmit={submit}>
      <label className="companion-page-reminder-text"><Icon name="calendar" size={17}/><input ref={input} aria-label="提醒内容" placeholder="明天上午九点提醒的事" maxLength={200} value={title} disabled={busy || Boolean(pending) || Boolean(source)} onChange={event => setTitle(event.target.value)}/></label>
      <div className="companion-page-reminder-schedule"><input type="datetime-local" aria-label="提醒时间 可留空解析原话" min={localInput(Date.now())} value={at} required={Boolean(source)} disabled={busy || Boolean(pending)} onChange={event => setAt(event.target.value)}/><button type="submit" className="companion-page-primary" disabled={disabled || !title.trim() || Boolean(source)&&!at}>{source?'改好':'定好'}</button></div>
      {source && <p className="companion-page-reminder-note">修改同一提醒的时间<button type="button" disabled={busy || Boolean(pending)} onClick={() => {setSource(null);setTitle('');setAt('');}}>取消</button></p>}
    </form>
    {pending && <div className="companion-page-reminder-recovery" role="status">{pending.invalid ? <><span>本机提交记录无法读取 · 原请求结果仍未知</span><button type="button" onClick={() => {removeStoredItem(slot);pendingRef.current=null;setPending(null);}}>清除无效本机记录</button></> : <>
      <span>{operation?.observed?.source_retired ? '原提醒已取消 · 后续尚未完成' : '本次操作尚未确认'}</span>
      {operation?.observed && <span>原提醒当前{({pending:'待提醒',cancelled:'已取消',completed:'已完成',acknowledged:'已确认'})[operation.observed.source_state] || '状态待核对'} · {operation.observed.occurrences.filter(item=>item.state==='presented').length}次已呈现 · {operation.observed.occurrences.filter(item=>['completed','acknowledged'].includes(item.state)).length}次已处理{operation.observed.planned_count ? ` · 原改期计划${operation.observed.planned_count}次 ${reminderTime(operation.observed.planned_at)}` : ''}</span>}
      <button type="button" disabled={busy} onClick={() => readOperation()}>回读本次提交</button>
      {(!operation || ['prepared','not_accepted'].includes(operation.state) || resume) && <button type="button" disabled={busy} onClick={() => send(pending,resume?'resume':'apply')}>{resume?'确认当前状态并继续':'继续本次提交'}</button>}
      <button type="button" disabled={busy} onClick={() => send(pending,'abandon')}>放弃本次提交</button>
    </>}</div>}
    {loadError && <p className="companion-page-error" role="status">{loadError}</p>}
    {(error || message) && <p className={error?'companion-page-error':'companion-page-reminder-note'} role="status">{error || message}</p>}
  </section>;
}
