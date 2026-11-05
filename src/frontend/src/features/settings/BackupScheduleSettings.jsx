import { useEffect, useRef, useState } from 'react';
import { Row, Icon } from '../../shared/ui';
import { deviceIdentity, isDeviceIdentity, userStorageKey } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { loadBackupSchedule, loadBackupScheduleCommand, newBackupCommand, saveBackupSchedule } from '../rebuild/localMemoryVaultApi';
import { backupScheduleFields, createBackupCommand, isBackupCommandSettled, readBackupCommands, saveBackupCommand, settleBackupCommand, validBackupSchedule } from './backupCommands';
import { reconcileBackupSchedule } from './backupScheduleState';

const fields = backupScheduleFields;
const names = { daily: '每天', weekly: '每周', monthly: '每月', off: '关闭' };
const days = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
const select = value => Object.fromEntries(fields.map(key => [key, value[key]]));
const same = (left, right) => left && right && fields.every(key => left[key] === right[key]);
const valid = validBackupSchedule;

export function BackupScheduleSettings({ scopeKey, blocked, refreshEpoch = 0 }) {
  const origin = globalThis.electronAPI?.backendBaseUrl || globalThis.location?.origin || '';
  const key = userStorageKey(`backup-schedule-operations:${origin}`), legacyKey = userStorageKey(`backup-schedule-command:${origin}`);
  const [initial] = useState(() => {
    try { return { commands: readBackupCommands(key, 'schedule', legacyKey), error: '' }; }
    catch (failure) { return { commands: [], error: failure.message }; }
  });
  const [pending, setPendingState] = useState(initial.commands[0] || null), pendingRef = useRef(pending);
  const [journalError, setJournalError] = useState(initial.error), [count, setCount] = useState(initial.commands.length);
  const [actual, setActual] = useState(null), [draft, setDraft] = useState(pending ? select(pending.body) : null);
  const [phase, setPhase] = useState(pending ? 'checking' : 'idle'), [open, setOpen] = useState(false);
  const [conflict, setConflict] = useState(false), [error, setError] = useState(''), [saved, setSaved] = useState(false);
  const [readEpoch, setReadEpoch] = useState(0), [loading, setLoading] = useState(false);
  const latest = useRef(null), observedRefresh = useRef(refreshEpoch), draftEdited = useRef(false);
  latest.current = { actual, draft, conflict };
  const gate = useRequestScope(scopeKey), scope = gate.scope, requests = useRef(new Set());
  const busy = loading || ['writing', 'checking'].includes(phase);
  function setPending(value) { pendingRef.current = value; setPendingState(value); }
  function advance(command, value, receipt = null) {
    settleBackupCommand(command);
    const commands = readBackupCommands(key, 'schedule', legacyKey), next = commands[0] || null;
    gate.invalidate('schedule-read');
    setPending(next); setCount(commands.length); setJournalError(''); setLoading(false); setPhase(next ? 'checking' : 'idle');
    const keepDraft = draftEdited.current && !same(latest.current.draft, command.body);
    const state = reconcileBackupSchedule({ ...latest.current, pending: next,
      draft: keepDraft ? latest.current.draft : select(next ? next.body : command.body) }, value);
    latest.current = state; draftEdited.current = Boolean(keepDraft);
    setActual(state.actual); setDraft(state.draft);
    const unchanged = receipt && value.revision === receipt.revision && same(value, receipt.settings);
    const nextConflict = !next && (state.conflict || !unchanged);
    latest.current = { ...state, conflict: nextConflict };
    setConflict(nextConflict); setSaved(!next && !state.conflict && Boolean(unchanged));
    setError(state.error || (next || unchanged ? '' : '设置已变化 · 草稿保留'));
  }
  function syncJournal(restart = false) {
    try {
      const commands = readBackupCommands(key, 'schedule', legacyKey), next = commands[0] || null;
      setJournalError(''); setCount(commands.length);
      if (commands.some(value => value.id === pendingRef.current?.id)) {
        if (restart) { setPhase('checking'); setReadEpoch(value => value + 1); }
        return true;
      }
      if (!pendingRef.current && commands.length === 0) return true;
      if (pendingRef.current && !isBackupCommandSettled(pendingRef.current)) throw new Error('当前保存记录已变化，请保留当前页面');
      gate.invalidate('schedule-write'); gate.invalidate('schedule-command'); gate.invalidate('schedule-read');
      for (const request of requests.current) request.abort();
      setLoading(false); setPending(next); setPhase(next ? 'checking' : 'idle');
      if (next && !draftEdited.current) {
        const nextDraft = select(next.body);
        latest.current = { ...latest.current, draft: nextDraft };
        setDraft(nextDraft);
      }
      const keepConflict = draftEdited.current && latest.current.conflict;
      latest.current = { ...latest.current, conflict: keepConflict };
      setConflict(keepConflict); setSaved(false); setError(keepConflict ? '设置已变化 · 草稿保留' : '');
      return true;
    } catch (failure) { setJournalError(failure.message); return false; }
  }
  const controller = () => { const value = new AbortController(); requests.current.add(value); return value; };
  useEffect(() => () => { for (const request of requests.current) request.abort(); }, []);
  useEffect(() => {
    const changed = event => { if (event.key === null || event.key === legacyKey || event.key?.startsWith(`${key}:operation:`)) syncJournal(); };
    globalThis.window?.addEventListener('storage', changed);
    return () => globalThis.window?.removeEventListener('storage', changed);
  }, [key, legacyKey, scope]);

  async function load(preserveDraft = true, acknowledge = false) {
    const token = gate.issue('schedule-read', scope), identity = deviceIdentity(), request = controller();
    setLoading(true);
    try {
      const value = await loadBackupSchedule(request.signal);
      if (!gate.isCurrent(token) || !isDeviceIdentity(identity)) return;
      if (!valid(value) || !Number.isSafeInteger(value.revision)) throw new Error('backup_schedule_invalid');
      const next = reconcileBackupSchedule({ ...latest.current, pending: pendingRef.current }, value, { preserveDraft, acknowledge });
      latest.current = next;
      if (same(next.draft, value)) draftEdited.current = false;
      setActual(next.actual); setDraft(next.draft); setConflict(next.conflict); setError(next.error); setSaved(false);
    } catch { if (gate.isCurrent(token) && isDeviceIdentity(identity)) setError('读取未完成'); }
    finally { requests.current.delete(request); if (gate.isCurrent(token) && isDeviceIdentity(identity)) setLoading(false); }
  }
  useEffect(() => { load(Boolean(pending)); }, [scope]);
  useEffect(() => {
    if (observedRefresh.current === refreshEpoch) return;
    observedRefresh.current = refreshEpoch;
    syncJournal();
    if (pendingRef.current) { setPhase('checking'); setReadEpoch(value => value + 1); }
    load(true);
  }, [refreshEpoch, scope]);

  useEffect(() => {
    if (!pending || phase !== 'checking') return;
    const token = gate.issue('schedule-command', scope), identity = deviceIdentity();
    let timer, request, disposed = false;
    const current = () => !disposed && gate.isCurrent(token) && isDeviceIdentity(identity) && pendingRef.current?.id === pending.id;
    const poll = async () => {
      request = controller();
      try {
        const receipt = await loadBackupScheduleCommand({ commandId: pending.id, signal: request.signal });
        if (!current()) return;
        if (receipt.command_id !== pending.id || receipt.state !== 'saved' || !same(receipt.settings, pending.body)
          || receipt.expected_revision !== pending.body.expected_revision || !Number.isSafeInteger(receipt.revision)
          || receipt.revision < 1) throw new Error('backup_schedule_result_invalid');
        const value = await loadBackupSchedule(request.signal);
        if (!current()) return;
        if (!valid(value) || !Number.isSafeInteger(value.revision)) throw new Error('backup_schedule_invalid');
        advance(pending, value, receipt); return;
      } catch (failure) {
        if (!current()) return;
        if (failure.status === 404 && failure.code === 'backup_command_not_found') {
          try {
            const value = await loadBackupSchedule(request.signal);
            if (!current()) return;
            if (!valid(value) || !Number.isSafeInteger(value.revision)) throw new Error('backup_schedule_invalid');
            // 更高修订永久阻止原 expected_revision 的迟到提交，草稿仍保留供重新核对。
            if (value.revision > pending.body.expected_revision) { advance(pending, value); return; }
            saveBackupCommand(pending, 'not_received'); setActual(value);
            setPhase('not_accepted'); setError('此次保存尚未受理'); return;
          } catch (reason) {
            if (current()) { setError(reason.message || '当前配置暂不可读'); timer = setTimeout(poll, 2000); }
            return;
          }
        }
        setError('保存结果暂不可读');
      } finally { requests.current.delete(request); }
      if (current()) timer = setTimeout(poll, 2000);
    };
    poll();
    return () => { disposed = true; clearTimeout(timer); request?.abort(); };
  }, [pending, phase, scope, readEpoch]);

  async function save() {
    if (blocked || busy || journalError || !draft || !valid(draft) || !actual || conflict) return;
    const prior = pendingRef.current;
    if (!syncJournal()) return;
    if (pendingRef.current?.id !== prior?.id) { load(true); return; }
    let command = pendingRef.current;
    if (!command) {
      const id = newBackupCommand();
      command = createBackupCommand('schedule', key, id, { ...select(draft), expected_revision: actual.revision, command_id: id });
    }
    try { command = saveBackupCommand(command, 'sending'); setCount(readBackupCommands(key, 'schedule', legacyKey).length); }
    catch (failure) { setJournalError(failure.message); return; }
    const token = gate.issue('schedule-write', scope), identity = deviceIdentity(), request = controller();
    setPending(command); setPhase('writing'); setError(''); setSaved(false);
    const current = () => gate.isCurrent(token) && isDeviceIdentity(identity) && pendingRef.current?.id === command.id;
    try {
      await saveBackupSchedule(command.body, request.signal);
      if (current()) setPhase('checking');
    } catch (failure) {
      if (current()) { setError(failure.code || '保存结果暂不可读'); setPhase('checking'); }
    } finally { requests.current.delete(request); }
  }
  function change(values) {
    draftEdited.current = true;
    const next = { ...latest.current.draft, ...values };
    latest.current = { ...latest.current, draft: next };
    setDraft(next); setSaved(false); setOpen(true);
  }
  const dirty = actual && draft && !same(actual, draft);
  let next = '—';
  if (actual?.next_at && Number.isFinite(new Date(actual.next_at).getTime())) next = new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: actual.timezone,
  }).format(new Date(actual.next_at));
  return <section className="settings-item">
    <Row dot={<span className="settings-glyph"><Icon name="calendar" size={18}/></span>} title="自动备份" expanded={open}
      sub={actual ? `${names[actual.frequency]} · ${actual.timezone === 'UTC' ? 'UTC' : '北京时间'}` : '读取中'}
      onOpen={() => setOpen(value => !value)} trailing={<div className="settings-support-inline">
        <div role="radiogroup" aria-label="自动备份周期" className="settings-support-inline">{Object.entries(names).map(([value, label]) =>
          <button key={value} type="button" role="radio" aria-checked={draft?.frequency === value}
            className={`settings-support-button${draft?.frequency === value ? ' is-primary' : ''}`}
            disabled={blocked || busy || journalError || !actual || Boolean(pending)} onClick={() => change({ frequency: value })}>{label}</button>)}</div>
        <button type="button" className="settings-time-pill" aria-label="修改自动备份时间" aria-expanded={open}
          disabled={blocked || busy || !actual} onClick={() => setOpen(value => !value)}>{actual?.time || '—'}</button>
        {dirty && !pending && <span role="status">待保存</span>}
      </div>}/>
    {open && <form className="settings-expand" onSubmit={event => { event.preventDefault(); save(); }}>
      <div className="settings-fields">
        <label>时间<input type="time" step="60" required value={draft?.time || ''} disabled={blocked || busy || Boolean(pending) || draft?.frequency === 'off'}
          onChange={event => change({ time: event.target.value })}/></label>
        <label>时区<select value={draft?.timezone || 'UTC'} disabled={blocked || busy || Boolean(pending)}
          onChange={event => change({ timezone: event.target.value })}><option value="UTC">UTC</option><option value="Asia/Shanghai">北京时间</option></select></label>
        {draft?.frequency === 'weekly' && <label>每周日期<select value={draft.weekday} disabled={blocked || busy || Boolean(pending)}
          onChange={event => change({ weekday: Number(event.target.value) })}>{days.map((label, value) => <option key={value} value={value}>{label}</option>)}</select></label>}
        {draft?.frequency === 'monthly' && <label>每月日期<input type="number" min="1" max="31" step="1" value={draft.month_day}
          disabled={blocked || busy || Boolean(pending)} onChange={event => change({ month_day: Number(event.target.value) })}/></label>}
      </div>
      <p className="settings-support-empty">计划时间 {next}{draft?.frequency === 'monthly' ? ' · 不足日期取月末' : ''}</p>
      <div className="settings-save"><button type="submit" disabled={blocked || busy || journalError || !valid(draft) || conflict || (!pending && !dirty)}>
        {phase === 'writing' ? '保存中' : phase === 'checking' ? '核对中' : phase === 'not_accepted' ? '重试本次保存' : saved ? '✓ 已保存' : '保存'}</button></div>
    </form>}
    {count > 1 && <p role="status">待核对 {count} 项</p>}
    {journalError && <p role="alert">{journalError} <button type="button" onClick={() => syncJournal(true)}>重新读取操作记录</button></p>}
    {conflict && actual && <p className="settings-support-empty">当前 {names[actual.frequency]}
      {actual.frequency === 'weekly' ? ` ${days[actual.weekday]}` : actual.frequency === 'monthly' ? ` ${actual.month_day} 日` : ''}
      {` · ${actual.time} · ${actual.timezone === 'UTC' ? 'UTC' : '北京时间'}`}</p>}
    {error && <p role="alert">{error} {pending ? <button type="button" disabled={busy}
      onClick={() => { setPhase('checking'); setReadEpoch(value => value + 1); }}>核对结果</button>
      : <button type="button" disabled={busy} onClick={() => load(true, true)}>重新核对配置</button>}</p>}
  </section>;
}
