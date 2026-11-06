import { useEffect, useId, useRef, useState } from 'react';
import { Icon, Row } from '../../shared/ui';
import { deviceIdentity, isDeviceIdentity, userStorageKey, readDeviceCredential } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { sharedProjectsApi as api } from './sharedProjectsApi';
import { clearSharedCommand, createSharedCommand, readSharedCommands, saveSharedCommand, validateSharedOperation,
  validateSharedAcknowledgement, sharedCommandBlocks, validateSharedCommand } from './sharedProjectCommands';
import { sharedProjectView, sharedInvitationTargets, canDecideSharedProject } from './sharedProjectViews';
import './sharedProjects.css';

const roles = { owner: '所有者', member: '成员', viewer: '只看成员', admin: '管理员' };
const actions = { create: '创建项目', invite: '邀请成员', accept: '接受邀请', role: '调整角色',
  remove: '移出成员', leave: '离开项目', transfer: '转让项目', delete: '删除项目', supersede: '确认取代',
  remember: '记住', file: '移入资料', consolidate: '每日整理' };
function panelCommands(key) {
  let rows;
  try { rows = readSharedCommands(key); }
  catch { throw new Error('待核对操作记录无法读取，请保留本机记录'); }
  return rows.map(row => validateSharedCommand(row, key));
}

export function SharedProjectsPanel({ enabled = false, projectId, onProjectsChanged, onOpenProject, onBackup }) {
  const [epoch, setEpoch] = useState(0), [selected, setSelected] = useState(projectId || null);
  const [projects, setProjects] = useState(null), [users, setUsers] = useState([]), [invitations, setInvitations] = useState([]);
  const [detail, setDetail] = useState(null), [activity, setActivity] = useState([]), [pending, setPending] = useState([]);
  const [error, setError] = useState(null), [status, setStatus] = useState(''), [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0), [projectName, setProjectName] = useState('');
  const [inviteUser, setInviteUser] = useState(''), [inviteRole, setInviteRole] = useState('member');
  const [activityUser, setActivityUser] = useState(''), [decision, setDecision] = useState(null);
  const [confirmation, setConfirmation] = useState(''), [skipBackup, setSkipBackup] = useState(false);
  const [commands, setCommands] = useState([]), [commandId, setCommandId] = useState(null);
  const [observed, setObserved] = useState({}), [journalError, setJournalError] = useState(false);
  const [loadedScope, setLoadedScope] = useState(null);
  const commandKey = userStorageKey('shared-project-command-v1');
  const gate = useRequestScope(`${enabled}\0${selected}\0${epoch}`), scope = gate.scope;
  const dialog = useRef(null), focus = useRef(null), inFlight = useRef(null);
  const decisionId = useId();
  const ready = loadedScope === scope;
  const project = ready ? detail?.project : null;
  const command = commands.find(row => row.id === commandId) || commands[0] || null;
  const projectBlocked = journalError || commands.some(row => row.projectId === project?.id
    || observed[row.id]?.project_id === project?.id || pending.some(operation => operation.operation_id === row.id)
    || row.expected.action === 'create' && !row.projectId && !observed[row.id]);
  const canManage = project?.state === 'active' && project.can_manage && !projectBlocked;
  const writable = ready && !busy && !journalError;
  const canResume = command && observed[command.id]?.state === 'pending'
    && projects?.some(row => row.id === observed[command.id].project_id);
  function reload() {
    setError(null);
    try { syncCommands(); } catch (reason) { setError(reason); setJournalError(true); }
    setRefresh(value => value + 1);
  }
  function syncCommands(preferred) {
    const rows = panelCommands(commandKey);
    setCommands(rows); setJournalError(false);
    setCommandId(old => rows.some(row => row.id === preferred) ? preferred : rows.some(row => row.id === old) ? old : rows[0]?.id || null);
    return rows;
  }
  useEffect(() => { setSelected(projectId || null); }, [projectId]);
  useEffect(() => {
    setInviteUser(''); setInviteRole('member'); setActivityUser(''); setDecision(null);
  }, [selected, enabled, epoch]);
  useEffect(() => {
    const changed = () => {
      gate.invalidate('read'); gate.invalidate('write');
      setEpoch(value => value + 1); setProjects(null); setDetail(null); setUsers([]); setInvitations([]);
      setActivity([]); setPending([]); setDecision(null); setConfirmation(''); setProjectName('');
      setInviteUser(''); setActivityUser(''); setError(null); setStatus(''); setBusy(false);
      setCommands([]); setCommandId(null); setObserved({}); setLoadedScope(null); setJournalError(false); inFlight.current = null;
    };
    window.addEventListener('chriptmas-user-space', changed); window.addEventListener('chriptmas-device-required', changed);
    return () => { window.removeEventListener('chriptmas-user-space', changed); window.removeEventListener('chriptmas-device-required', changed); };
  }, [gate]);
  useEffect(() => {
    if (enabled !== true) {
      gate.invalidate('read'); gate.invalidate('write'); inFlight.current = null;
      setProjects(null); setDetail(null); setDecision(null); setCommands([]); setCommandId(null);
      setObserved({}); setLoadedScope(null); setError(null); setStatus(''); setBusy(false);
      setConfirmation(''); setProjectName(''); setInviteUser(''); setActivityUser('');
      return;
    }
    const changed = event => {
      if (event?.type === 'storage' && event.key !== null && !event.key.startsWith(`${commandKey}:operation:`)) return;
      try { syncCommands(); } catch (reason) { setError(reason); setJournalError(true); }
    };
    changed(); window.addEventListener('storage', changed); window.addEventListener('focus', changed);
    return () => { window.removeEventListener('storage', changed); window.removeEventListener('focus', changed); };
  }, [enabled, commandKey, epoch]);
  useEffect(() => {
    if (enabled !== true) return;
    const request = gate.issue('read', scope), identity = deviceIdentity(), controller = new AbortController();
    setDetail(null); setActivity([]); setPending([]); setDecision(null);
    if (loadedScope !== scope) setError(null);
    async function read() {
      try {
        const options = { signal: controller.signal };
        const [rows, directory, incoming] = await Promise.all([api.list(options), api.users(options), api.invitations(options)]);
        if (!gate.isCurrent(request) || !isDeviceIdentity(identity)) return;
        setProjects(rows); setUsers(directory); setInvitations(incoming); setLoadedScope(scope);
        if (selected) {
          const data = await api.detail(selected, options);
          const [history, operations] = await Promise.all([
            data.project.state === 'active' ? api.activity(selected, activityUser, options) : Promise.resolve([]),
            data.project.can_manage ? api.pending(selected, options) : Promise.resolve([]),
          ]);
          if (gate.isCurrent(request) && isDeviceIdentity(identity)) {
            setDetail(data); setActivity(history); setPending(operations);
          }
        }
      } catch (reason) { if (gate.isCurrent(request) && isDeviceIdentity(identity)) setError(reason); }
    }
    read(); return () => controller.abort();
  }, [enabled, selected, epoch, refresh, activityUser]);
  useEffect(() => {
    if (decision) { focus.current = document.activeElement; dialog.current?.showModal(); }
    else { dialog.current?.close(); if (focus.current?.isConnected) focus.current.focus(); }
    return () => { if (dialog.current?.open) dialog.current.close(); };
  }, [decision]);
  async function act(spec, options = {}, intent = 'new', operationId = command?.id) {
    if (enabled !== true || busy || inFlight.current || !ready || journalError) return;
    const request = gate.issue('write', scope), identity = deviceIdentity();
    if (!request) return;
    inFlight.current = request;
    setBusy(true); setError(null); setStatus('');
    let saved;
    try {
      const current = () => gate.isCurrent(request) && isDeviceIdentity(identity);
      const actorUserId = readDeviceCredential()?.device?.user_id;
      if (!actorUserId) throw new Error('请重新登录');
      const rows = panelCommands(commandKey);
      const resuming = intent === 'resume' || spec?.kind === 'resume';
      const existing = spec?.kind === 'resume' ? rows.find(row => row.id === spec.operation.operation_id)
        : intent !== 'new' ? rows.find(row => row.id === operationId) : null;
      if (intent !== 'new' && !existing) throw new Error('待核对操作已处理，请重新读取');
      if (intent === 'new' && spec?.kind !== 'resume') {
        if (spec.project && projectBlocked) throw new Error('当前项目仍有待核对操作，请先读取原操作');
        if (spec.kind === 'invite' && !sharedInvitationTargets(users, detail).some(user=>user.user_id===spec.userId))
          throw new Error('请重新选择可邀请用户');
        const blocking = rows.find(row => sharedCommandBlocks(row, spec));
        if (blocking) { syncCommands(blocking.id); throw new Error('该项目已有待核对操作，请先处理原操作'); }
      }
      saved = existing || saveSharedCommand(validateSharedCommand(createSharedCommand({ ...spec, actorUserId }, commandKey, options), commandKey));
      syncCommands(saved.id);
      const keep = phase => { saved = saveSharedCommand({ ...saved, phase }); syncCommands(saved.id); };
      function complete(value) {
        if (!current()) return;
        if (!['completed', 'cancelled'].includes(value.state)) throw new Error('操作尚未完成');
        clearSharedCommand(saved); syncCommands();
        setDecision(null); setConfirmation(''); setSkipBackup(false);
        setStatus(`${actions[saved.expected.action] || '操作'}${value.state === 'cancelled' ? '已取消' : '已完成'}`);
        if (saved.options?.clearName && projectName === saved.body.project_name) setProjectName('');
        if (saved.expected.action === 'invite' && inviteUser === saved.body.user_id) setInviteUser('');
        if (saved.options?.leave && selected === saved.projectId) setSelected(null);
        setRefresh(value => value + 1); onProjectsChanged?.();
      }
      async function observe() {
        const value = validateSharedOperation(saved, await api.operation(saved.id), actorUserId);
        if (current()) setObserved(old => ({ ...old, [saved.id]: value }));
        return value;
      }
      if (existing || spec?.kind === 'resume') {
        let operation;
        try { operation = await observe(); }
        catch (reason) {
          if (reason.status !== 404 || reason.code !== 'shared_operation_not_received') throw reason;
          if (!current()) return;
          keep('not_received');
        }
        if (!current()) return;
        if (operation?.state === 'completed' || operation?.state === 'cancelled') { complete(operation); return; }
        if (operation) {
          keep('pending');
          if (!resuming) { setStatus('上次操作已受理，仍待完成'); return; }
        } else if (intent === 'cancel') {
          operation = validateSharedOperation(saved, await api.cancelUnreceived(saved), actorUserId);
          if (!current()) return;
          if (operation.state === 'cancelled' || operation.state === 'completed') complete(operation);
          else { keep('pending'); setStatus('上次操作已受理'); }
          return;
        } else if (intent !== 'retry') { setStatus('上次操作尚未受理，可重试原操作或取消'); return; }
      }
      keep('sending');
      const acknowledgement = validateSharedAcknowledgement(saved, resuming ? await api.resume(saved.id) : await api.sendCommand(saved));
      if (!current()) return;
      const value = await observe();
      if (!current()) return;
      if (value.project_id !== (acknowledgement.project?.id || acknowledgement.project_id)) throw new Error('提交结果的项目不一致，请读取原操作');
      if (value?.state === 'pending') {
        keep('pending');
        if (saved.options?.leave && selected === saved.projectId) setSelected(null);
        setStatus('操作已受理，仍待完成'); setRefresh(value => value + 1); onProjectsChanged?.();
      } else complete(value);
    } catch (reason) {
      if (gate.isCurrent(request) && isDeviceIdentity(identity)) {
        if (saved) {
          try { saveSharedCommand({ ...saved, phase: 'unknown' }); syncCommands(saved.id); }
          catch (storageError) { setJournalError(true); setError(storageError); return; }
        }
        setError(reason); setDecision(null); setRefresh(value => value + 1);
      }
    } finally {
      if (inFlight.current === request) { inFlight.current = null; setBusy(false); }
    }
  }
  function decide(action, member) {
    if (!ready || busy || projectBlocked || !canDecideSharedProject(project, member, action, users)) return;
    setConfirmation(''); setSkipBackup(false);
    setDecision({ action, member: member ? structuredClone(member) : null, project: structuredClone(project),
      scope, identity: deviceIdentity() });
  }
  function confirmDecision() {
    if (!decision || decision.scope !== scope || !isDeviceIdentity(decision.identity)
        || !project || project.id !== decision.project.id
        || JSON.stringify(project.authority) !== JSON.stringify(decision.project.authority)
        || !canDecideSharedProject(project, decision.member, decision.action, users)
        || decision.member && !detail.members.some(member=>member.user_id===decision.member.user_id && member.revision===decision.member.revision)) return;
    if (decision.action === 'delete' && (confirmation !== decision.project.name || !skipBackup)) return;
    if (decision.action === 'delete') act({ kind: 'delete', project: decision.project, confirmation }, { leave: true });
    else act({ kind: decision.action, project: decision.project, member: decision.member },
      { leave: decision.action === 'leave' });
  }
  if (enabled !== true) return null;
  return <section className="shared-projects-panel" aria-label="共享项目">
    <header><h2>共享项目</h2><button type="button" disabled={busy} onClick={reload}>刷新</button></header>
    {error && <div role="alert" className="shared-projects-error"><span>{error.message}</span>
      <button type="button" disabled={busy} onClick={reload}>重新读取</button></div>}
    {commands.length > 1 && <label>待核对操作<select aria-label="选择待核对操作" disabled={busy} value={command?.id || ''}
      onChange={event => setCommandId(event.target.value)}>{commands.map(row => <option key={row.id} value={row.id}>
        {row.label} · {actions[row.expected.action] || '操作'} · {row.id}</option>)}</select></label>}
    {command && <div className="shared-projects-error" role="status"><span>{command.label} · {actions[command.expected.action] || '操作'}待核对</span>
      <button type="button" disabled={!writable} onClick={() => act(null, {}, 'check', command.id)}>核对所选操作</button>
      {command.phase === 'pending' && canResume && <button type="button" disabled={!writable} onClick={() => act(null, {}, 'resume', command.id)}>继续已受理操作</button>}
      {command.phase === 'pending' && observed[command.id] && !canResume && <span>当前已无项目访问资格，活动同步需由所有者处理</span>}
      {command.phase === 'not_received' && <><button type="button" disabled={busy} onClick={() => act(null, {}, 'retry')}>重试原操作</button>
        <button type="button" disabled={busy} onClick={() => act(null, {}, 'cancel')}>取消未受理操作</button></>}
    </div>}
    {status && <p role="status">{status}</p>}
    <form onSubmit={event => { event.preventDefault(); act({ kind: 'create', projectName }, { clearName: true }); }}>
      <label>项目名称<input value={projectName} maxLength={80} disabled={busy} onChange={event => setProjectName(event.target.value)}/></label>
      <button disabled={!writable || !projectName.trim() || commands.some(row => sharedCommandBlocks(row, {kind:'create',projectName}))} type="submit">创建共享项目</button>
    </form>
    {!ready && !error && <p role="status">读取中</p>}
    {ready && projects?.map(row => <Row key={row.id} title={row.name} sub={`${row.owner_name || ''} · ${roles[row.role]} · ${row.id}${row.state === 'active' ? '' : ' · 待完成'}`}
      selected={selected === row.id} onOpen={() => { if (!busy) setSelected(row.id); }}
      trailing={row.state === 'active' && onOpenProject && <button type="button" disabled={busy}
        aria-label={`打开共享项目 ${row.name} ${row.id}`} onClick={() => onOpenProject(sharedProjectView(row))}>打开<Icon name="open" size={14}/></button>}/>)}
    {ready && projects?.length === 0 && <p>还没有共享项目</p>}
    {ready && invitations.map(invitation => <Row key={invitation.id} title={invitation.project_name} sub={`邀请你成为${roles[invitation.role]} · ${invitation.project_id}`} readOnly
      trailing={<button type="button" disabled={!writable || commands.some(row => sharedCommandBlocks(row,{kind:'accept',invitation}))} onClick={() => act({ kind: 'accept', invitation })}>接受邀请</button>}/>)}
    {project && <section aria-label={`${project.name}成员`}>
      <header><h3>{project.name}</h3><span>{roles[project.role]}</span></header>
      {onBackup && project.state === 'active' && <button type="button" disabled={!writable || projectBlocked}
        onClick={() => onBackup({ id: project.id, name: project.name })}>备份此项目</button>}
      {detail.members.map(member => <Row key={member.user_id} title={member.name || member.user_id} sub={`${roles[member.role]} · ${member.user_id}`} readOnly trailing={<div className="shared-projects-actions">
        {project.can_manage && project.state === 'active' && member.role !== 'owner' && <><select aria-label={`${member.name || '成员'} ${member.user_id}的角色`} disabled={!writable || !canManage} value={member.role}
          onChange={event => { const nextRole = event.target.value; act({ kind: 'role', project, member, role: nextRole }); }}>
          <option value="member">成员</option><option value="viewer">只看成员</option></select>
          <button type="button" aria-label={`转让给${member.name || '成员'} ${member.user_id}`} disabled={!writable || !canManage || !users.some(user => user.user_id === member.user_id)} onClick={() => decide('transfer', member)}>转让</button>
          <button type="button" aria-label={`移出${member.name || '成员'} ${member.user_id}`} disabled={!writable || !canManage} onClick={() => decide('remove', member)}>移出</button></>}
        {project.state === 'active' && member.user_id === project.actor_user_id && member.role !== 'owner'
          && <button type="button" disabled={!writable || projectBlocked} onClick={() => decide('leave', member)}>离开项目</button>}
      </div>}/>)}
      {project.can_manage && project.state === 'active' && <form onSubmit={event => { event.preventDefault(); act({ kind: 'invite', project, userId: inviteUser, role: inviteRole }); }}>
        <label>邀请用户<select value={inviteUser} disabled={!writable || !canManage} onChange={event => setInviteUser(event.target.value)}><option value="">选择用户</option>
          {sharedInvitationTargets(users, detail).map(user => <option key={user.user_id} value={user.user_id}>{user.name} · {user.user_id}</option>)}</select></label>
        <label>角色<select value={inviteRole} disabled={!writable || !canManage} onChange={event => setInviteRole(event.target.value)}><option value="member">成员</option><option value="viewer">只看成员</option></select></label>
        <button type="submit" disabled={!writable || !canManage || !sharedInvitationTargets(users, detail).some(user=>user.user_id===inviteUser)}>邀请</button>
      </form>}
      {detail.invitations.map(invitation => <p key={invitation.id}>{users.find(user => user.user_id === invitation.to_user_id)?.name || invitation.to_user_id} · 待接受</p>)}
      {pending.map(operation => <Row key={operation.operation_id} title={`${actions[operation.action] || operation.action}待完成`} readOnly
        trailing={<button type="button" disabled={busy} onClick={() => act({ kind: 'resume', operation })}>继续</button>}/>)}
      {project.state === 'active' && <><header><h3>活动记录</h3><label>成员<select aria-label="筛选活动成员" disabled={busy} value={activityUser} onChange={event => setActivityUser(event.target.value)}><option value="">全部</option>{users.map(user => <option key={user.user_id} value={user.user_id}>{user.name}</option>)}</select></label></header>
        {activity.map(item => <Row key={item.id} title={`${users.find(user => user.user_id === item.actor_user_id)?.name || item.actor_user_id} · ${actions[item.action] || item.action}`}
          sub={item.object_title} meta={new Date(item.at).toLocaleString()} readOnly/>)}</>}
      {project.can_manage && project.state === 'active' && <button type="button" disabled={!writable || !canManage} onClick={() => decide('delete')}>删除项目</button>}
    </section>}
    <dialog ref={dialog} className="shared-projects-dialog" aria-labelledby={decisionId} aria-describedby={`${decisionId}-description`}
      onCancel={event => { event.preventDefault(); setDecision(null); }}>
      <h3 id={decisionId}>{actions[decision?.action]}</h3>
      {decision && <p>{decision.project.name} · {decision.project.id}</p>}
      <p id={`${decisionId}-description`}>{!decision ? null : decision.action === 'delete' ? '项目资料将永久删除。输入项目名称确认。'
        : decision?.action === 'transfer' ? `将所有权交给${decision.member.name || '成员'} · ${decision.member.user_id}，原所有者成为成员。`
          : decision?.action === 'leave' ? '离开后失去项目访问权限，已有贡献保留。' : `${decision?.member.name || '成员'} · ${decision?.member.user_id}将失去项目访问权限，已有贡献保留。`}</p>
      {decision?.action === 'delete' && <><label>项目名称<input value={confirmation} disabled={busy} onChange={event => setConfirmation(event.target.value)}/></label>
        <label><input type="checkbox" checked={skipBackup} disabled={busy} onChange={event => setSkipBackup(event.target.checked)}/>不保留备份并永久删除</label></>}
      <div className="shared-projects-actions"><button type="button" disabled={!writable || projectBlocked || decision?.action === 'delete' && (confirmation !== decision.project.name || !skipBackup)} onClick={confirmDecision}>确认</button>
        <button type="button" autoFocus onClick={() => setDecision(null)}>{busy ? '关闭' : '取消'}</button></div>
    </dialog>
  </section>;
}
