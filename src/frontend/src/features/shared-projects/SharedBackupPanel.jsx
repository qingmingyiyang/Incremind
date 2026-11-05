import { useEffect, useRef, useState } from 'react';
import { FocusPanel } from '../../shared/ui/FocusPanel';
import { Row } from '../../shared/ui/Row';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { deviceIdentity, isDeviceIdentity, userStorageKey } from '../../shared/api/deviceTransport';
import { sharedBackupApi as api } from './sharedBackupApi';
import { sharedBackupCommands, newSharedBackupCommand, markSharedBackupCommand } from './sharedBackupCommands';
import './sharedProjects.css';

const labels = { preparing: '准备未完成', archived: '等待记录同步', ready: '已保留', failed: '备份失败', copying: '复制未完成', completed: '已恢复' };
const terminal = (command, value) => command.expected.kind === 'backup' ? ['ready', 'failed'].includes(value?.state) : value?.state === 'completed';

export function SharedBackupPanel({ enabled = false, request, refreshToken = 0 }) {
  const [epoch, setEpoch] = useState(0), [refresh, setRefresh] = useState(0);
  const [catalog, setCatalog] = useState(null), [journal, setJournal] = useState(null), [observed, setObserved] = useState(null);
  const [selected, setSelected] = useState(null), [form, setForm] = useState(null), [opened, setOpened] = useState(null);
  const [name, setName] = useState(''), [approved, setApproved] = useState(false), [error, setError] = useState(null);
  const [working, setWorking] = useState(false), active = useRef(null), seenRequest = useRef(null), network = useRef(null);
  const storageKey = userStorageKey('shared-backups-v1'), identity = deviceIdentity();
  const gate = useRequestScope(`${enabled}\0${storageKey}\0${epoch}`), scope = gate.scope;
  const inScope = () => enabled && gate.isScopeCurrent(scope) && isDeviceIdentity(identity) && userStorageKey('shared-backups-v1') === storageKey;
  const current = token => inScope() && gate.isCurrent(token);
  const rows = catalog?.scope === scope ? catalog.rows : [];
  const commands = journal?.scope === scope ? journal.rows : [];
  const values = observed?.scope === scope ? observed.values : {};
  const command = commands.find(row => row.id === selected), value = command && Object.hasOwn(values, command.id) ? values[command.id] : null;
  const visibleForm = form?.scope === scope ? form : null, open = opened === scope;
  const warn = reason => setError({ scope, message: reason.message || '操作未完成，请重新核对' });
  function sync(preferred) {
    const saved = sharedBackupCommands(storageKey);
    setJournal({ scope, rows: saved });
    if (preferred) setSelected(preferred);
    return saved;
  }
  function receive(saved, result) {
    const previous = Object.hasOwn(values, saved.id) ? values[saved.id] : null;
    if (saved.expected.kind === 'restore' && previous && previous.project_id !== result.project_id)
      throw new Error('恢复目标已变化，请保留原操作编号');
    markSharedBackupCommand(saved, 'pending');
    setObserved(old => ({ scope, values: { ...(old?.scope === scope ? old.values : {}), [saved.id]: result } }));
    sync();
  }
  async function operate(saved, send = false) {
    if (active.current || !inScope()) return;
    const token = gate.issue('operation', scope);
    if (!token) return;
    const controller = new AbortController(); network.current = controller;
    active.current = token; setWorking(true); setError(null);
    try {
      if (send) markSharedBackupCommand(saved, 'unknown');
      const result = await (send ? api.send(saved, controller.signal) : api.read(saved, controller.signal));
      if (!current(token)) return;
      receive(saved, result);
      if (saved.expected.kind === 'backup' && result.state === 'ready') setRefresh(number => number + 1);
    } catch (reason) {
      if (!current(token)) return;
      if (!send && reason.status === 404 && ['shared_backup_unavailable', 'shared_backup_import_unavailable'].includes(reason.code)) {
        try { markSharedBackupCommand(saved, 'not_received'); sync(); }
        catch (failure) { warn(failure); return; }
        setObserved(old => ({ scope, values: { ...(old?.scope === scope ? old.values : {}), [saved.id]: null } }));
        warn(new Error('尚未读取到结果，可使用原编号重试'));
      } else warn(reason);
    } finally {
      if (network.current === controller) network.current = null;
      if (current(token) && active.current === token) { active.current = null; setWorking(false); }
    }
  }
  useEffect(() => {
    network.current?.abort(); network.current = null; active.current = null; setWorking(false);
  }, [scope]);
  useEffect(() => () => network.current?.abort(), []);
  useEffect(() => {
    const changed = () => {
      gate.invalidate('operation'); gate.invalidate('catalog'); gate.invalidate('download');
      network.current?.abort(); network.current = null;
      active.current = null; seenRequest.current = null; setEpoch(number => number + 1);
      setCatalog(null); setJournal(null); setObserved(null); setSelected(null); setForm(null); setOpened(null);
      setName(''); setApproved(false); setError(null); setWorking(false);
    };
    window.addEventListener('chriptmas-user-space', changed); window.addEventListener('chriptmas-device-required', changed);
    return () => { window.removeEventListener('chriptmas-user-space', changed); window.removeEventListener('chriptmas-device-required', changed); };
  }, [gate]);
  useEffect(() => {
    if (!inScope()) return;
    const token = gate.issue('catalog', scope), controller = new AbortController();
    setCatalog(null);
    try { sync(); } catch (reason) { warn(reason); }
    api.owned(controller.signal).then(backups => {
      if (current(token)) setCatalog({ scope, rows: backups });
    }).catch(reason => { if (current(token)) warn(reason); });
    return () => { controller.abort(); gate.invalidate('catalog'); };
  }, [enabled, scope, refresh, refreshToken]);
  useEffect(() => {
    if (!enabled || !request || request.scope !== storageKey || seenRequest.current === request.nonce) return;
    seenRequest.current = request.nonce;
    if (active.current) { warn(new Error('请先核对当前备份或恢复操作')); return; }
    setForm({ scope, kind: 'backup', project: request.project }); setSelected(null); setOpened(scope); setError(null);
  }, [enabled, scope, request]);
  useEffect(() => {
    if (enabled && command && !active.current) operate(command);
  }, [enabled, scope, selected]);
  useEffect(() => {
    const changed = event => {
      if (!enabled || !gate.isScopeCurrent(scope) || !isDeviceIdentity(identity)
          || event.key && !event.key.startsWith(`${storageKey}:operation:`)) return;
      try { sync(); } catch (reason) { warn(reason); }
    };
    window.addEventListener('storage', changed);
    return () => window.removeEventListener('storage', changed);
  }, [enabled, scope, storageKey]);

  function known(saved) {
    return (Object.hasOwn(values, saved.id) ? values[saved.id] : null) || (saved.expected.kind === 'backup'
      ? rows.find(row => row.command_id === saved.id && row.project_id === saved.expected.source) : null);
  }
  function start(kind, source, label, requestedName) {
    if (active.current || !inScope()) return;
    try {
      const unresolved = commands.find(saved => saved.expected.kind === kind && saved.expected.source === source && !terminal(saved, known(saved)));
      if (unresolved) {
        setSelected(unresolved.id); setForm(null); setOpened(scope);
        warn(new Error('请先核对已有操作，再创建新的副本')); return;
      }
      const saved = newSharedBackupCommand(kind, source, label, storageKey, requestedName);
      sync(saved.id); setForm(null); setOpened(scope); operate(saved, true);
    } catch (reason) { warn(reason); }
  }
  function selectBackup(backup) {
    if (active.current || !inScope()) return;
    setError(null); setOpened(scope);
    if (backup.state === 'ready') {
      setForm({ scope, kind: 'restore', backup }); setSelected(null); setName(backup.project_name); setApproved(false);
    } else {
      try {
        const saved = commands.find(item => item.id === backup.command_id)
          || newSharedBackupCommand('backup', backup.project_id, backup.project_name, storageKey, undefined, backup.command_id);
        sync(saved.id); setForm(null);
        if (selected === saved.id) operate(saved);
      } catch (reason) { warn(reason); }
    }
  }
  async function download(backup) {
    if (active.current || !inScope()) return;
    const token = gate.issue('download', scope);
    if (!token) return;
    const controller = new AbortController(); network.current = controller;
    active.current = token; setWorking(true); setError(null);
    try { await api.download(backup, () => current(token), controller.signal); }
    catch { if (current(token)) warn(new Error('备份下载未完成，请核对原备份后重试')); }
    finally {
      if (network.current === controller) network.current = null;
      if (current(token) && active.current === token) { active.current = null; setWorking(false); }
    }
  }
  if (!enabled) return null;
  return <section className="shared-projects-panel" aria-label="当前空间共享备份">
    <header><h3>当前空间保留的共享备份</h3><button type="button" disabled={working} onClick={() => setRefresh(number => number + 1)}>刷新</button></header>
    {catalog?.scope !== scope && error?.scope !== scope && <p role="status">读取中</p>}
    {catalog?.scope === scope && rows.length === 0 && <p>还没有保留的共享备份</p>}
    {rows.map(backup => <Row key={backup.command_id} title={backup.project_name}
      sub={new Date(backup.created_at).toLocaleString()} meta={labels[backup.state]}
      dot={backup.state === 'ready' ? 'done' : backup.state === 'failed' ? 'failed' : 'processing'}
      onOpen={() => selectBackup(backup)} trailing={backup.state === 'ready' && <div className="shared-projects-actions">
        <button type="button" disabled={working} aria-label={`下载${backup.project_name}备份`} onClick={() => download(backup)}>下载 ZIP</button>
        <button type="button" disabled={working} onClick={() => selectBackup(backup)}>恢复为个人项目</button>
      </div>}/>)}
    {!!commands.length && <label>备份与恢复记录<select aria-label="备份与恢复记录" value={selected || ''} disabled={working}
      onChange={event => { setSelected(event.target.value || null); setForm(null); setOpened(scope); setError(null); }}>
      <option value="">选择记录</option>{commands.map(saved => <option key={saved.id} value={saved.id}>{saved.label} · {saved.expected.kind === 'backup' ? '备份' : '恢复'} · {saved.id}</option>)}
    </select></label>}
    {command && <button type="button" disabled={working} onClick={() => { setForm(null); setOpened(scope); operate(command); }}>查看所选操作</button>}
    {error?.scope === scope && !open && <p role="alert">{error.message}</p>}
    {working && !open && <p role="status">正在读取或保存</p>}
    {open && <FocusPanel title="共享项目备份" className="shared-extraction-focus shared-backup-focus" onClose={() => setOpened(null)}>
      {error?.scope === scope && <p role="alert">{error.message}</p>}
      {visibleForm?.kind === 'backup' && <div className="shared-backup-form">
        <Row readOnly title={visibleForm.project.name} sub="完整共享资料将保留到当前用户空间"/>
        <button type="button" disabled={working} onClick={() => start('backup', visibleForm.project.id, visibleForm.project.name)}>创建备份</button>
      </div>}
      {visibleForm?.kind === 'restore' && <form className="shared-backup-form" onSubmit={event => {
        event.preventDefault(); if (approved) start('restore', visibleForm.backup.command_id, visibleForm.backup.project_name, name);
      }}>
        <Row readOnly title={visibleForm.backup.project_name} sub={new Date(visibleForm.backup.created_at).toLocaleString()}/>
        <label>新个人项目名称<input value={name} maxLength={80} disabled={working} onChange={event => setName(event.target.value)}/></label>
        <label><input type="checkbox" checked={approved} disabled={working} onChange={event => setApproved(event.target.checked)}/>确认将完整资料恢复为当前空间的新个人项目</label>
        <button type="submit" disabled={working || !approved || !name.trim()}>确认恢复</button>
      </form>}
      {!visibleForm && command && <section aria-label="备份恢复进度">
        <Row readOnly title={command.label} meta={value ? labels[value.state] : '结果待核对'}/>
        <p>操作编号 <code>{command.id}</code></p>
        {command.expected.kind === 'restore' && value && <>
          {(value.total > 0 || value.state !== 'completed') && <progress aria-label="已保留资料" max={Math.max(1, value.total)} value={value.total ? value.completed : undefined}/>}
          <p>{value.total ? `${value.completed}/${value.total}` : value.state === 'completed' ? '项目已恢复' : '完整资料核对未完成'}</p>
          {value.requires_privacy_review && <p role="status">{value.state === 'completed'
            ? '部分资料已保留为私密，请在新项目复核' : '部分资料需保留为私密，恢复完成后请在新项目复核'}</p>}
          {value.state === 'completed' && <a href={`#${new URLSearchParams({ view: 'library', project_id: value.project_id })}`}>查看个人项目 {value.name}</a>}
        </>}
        {command.expected.kind === 'backup' && value?.state === 'ready' && <button type="button" disabled={working} onClick={() => download(value)}>下载 ZIP</button>}
        {command.expected.kind === 'backup' && value?.state === 'failed' && <>
          <p>该次备份失败，重新备份需要当前项目访问资格</p>
          <button type="button" disabled={working} onClick={() => { setForm({ scope, kind: 'backup', project: { id: value.project_id, name: value.project_name } }); setSelected(null); }}>重新备份</button>
        </>}
        <div className="shared-projects-actions">
          <button type="button" disabled={working} onClick={() => operate(command)}>核对原操作</button>
          {value && !terminal(command, value) && <button type="button" disabled={working} onClick={() => operate(command, true)}>继续原操作</button>}
          {!value && command.phase === 'not_received' && <button type="button" disabled={working} onClick={() => operate(command, true)}>使用原编号重试</button>}
        </div>
      </section>}
      {working && <p role="status">正在保存或读取，关闭面板后仍可通过原操作记录核对</p>}
    </FocusPanel>}
  </section>;
}
