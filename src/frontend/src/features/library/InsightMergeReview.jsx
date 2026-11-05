import { useEffect, useRef, useState } from 'react';
import { deviceIdentity, isDeviceIdentity, userStorageKey } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { recognitionApi } from '../../shared/api/recognitionApi';
import { libraryMergeApi, mergeCommands, beginMergeCommand, validateMergeOperation,
  markMergeCommand, finishMergeCommand } from './libraryMergeApi';

export function InsightMergeReview({ projectId, detail, readonly, onBusy, onChanged, onOpenTarget, shared }) {
  const identity = deviceIdentity(), storage = shared?.storage || userStorageKey('library-merges-v1:' + projectId);
  const transport = shared?.api || libraryMergeApi;
  const gate = useRequestScope(`${storage}\0${identity.key}\0${identity.revision}`), scope = gate.scope;
  const [state, setState] = useState(null), [refresh, setRefresh] = useState(0);
  const [targets, setTargets] = useState(null), [selectedTarget, setSelectedTarget] = useState('');
  const [targetError, setTargetError] = useState(''), [targetRefresh, setTargetRefresh] = useState(0);
  const pending = useRef(null), callbacks = useRef(null);
  callbacks.current = { onChanged, onBusy };
  const value = state?.scope === scope ? state : null;
  const commands = value?.commands || [], command = commands.find(row => row.id === value?.selected) || commands[0];
  const operation = value?.operation, busy = Boolean(value?.busy);
  const candidate = detail?.insight, target = shared ? targets?.find(row => row.id === selectedTarget) : candidate?.hint?.target;
  const duplicate = candidate?.state === 'pending' && (shared || candidate?.hint?.relation === 'duplicate_of') && target;
  const permissionFields = detail && ['insight_project_id','can_write','can_edit_insight'].some(key=>Object.hasOwn(detail,key));
  const canWrite = !readonly && (permissionFields ? detail.can_write === true && detail.can_edit_insight === true
    && detail.insight_project_id === projectId : !detail?.readonly);
  const eligible = duplicate && canWrite && target.project_id === projectId
    && (detail.insight_project_id || projectId) === projectId;
  const manual = Boolean(shared && canWrite && candidate?.state === 'pending');
  const conditions = [...new Set([...(candidate?.conditions || []), ...(target?.conditions || [])])];
  useEffect(() => {
    setTargets(null); setSelectedTarget(''); setTargetError('');
    if (!manual) return;
    const controller = new AbortController(), token = gate.issue('targets', scope);
    transport.targets(controller.signal).then(rows => {
      if (gate.isCurrent(token) && isDeviceIdentity(identity)) setTargets(rows);
    }).catch(reason => {
      if (!controller.signal.aborted && gate.isCurrent(token) && isDeviceIdentity(identity)) setTargetError(reason.message);
    });
    return () => { controller.abort(); gate.invalidate('targets'); };
  }, [manual, scope, candidate?.id, candidate?.revision, targetRefresh]);
  useEffect(() => { callbacks.current.onBusy?.(busy); return () => callbacks.current.onBusy?.(false); }, [busy]);
  useEffect(() => {
    pending.current = null;
    gate.invalidate('operation');
    try {
      const commands = mergeCommands(storage, projectId);
      setState({ scope, commands, selected: commands[0]?.id, operation: null, error: '', busy: false });
    } catch (reason) { setState({ scope, commands: [], error: reason.message, blocked: true, busy: false }); }
    const changed = event => { if (!event.key || event.key.startsWith(storage)) setRefresh(value => value + 1); };
    window.addEventListener('storage', changed);
    return () => { gate.invalidate('operation'); window.removeEventListener('storage', changed); };
  }, [scope, refresh]);

  async function run(selected, action) {
    if (pending.current || !selected || !isDeviceIdentity(identity)) return;
    const token = gate.issue('operation', scope);
    if (!token) return;
    pending.current = token;
    setState(old => ({ ...old, busy: true, operation: null, error: '' }));
    try {
      const result = validateMergeOperation(await action(), selected);
      if (!gate.isCurrent(token) || !isDeviceIdentity(identity)) return;
      const terminal = result.state === 'cancelled' || result.proposal.state !== 'pending';
      if (terminal) finishMergeCommand(selected); else markMergeCommand({ ...selected, phase: 'pending' });
      const remaining = mergeCommands(storage, projectId);
      setState({ scope, commands: remaining, selected: terminal ? remaining[0]?.id : selected.id,
        operation: terminal ? null : result, error: '', busy: false,
        notice: terminal ? result.state === 'cancelled' ? '未受理操作已取消' : result.proposal.state === 'approved' ? '已合并 · 仍待确认' : '已忽略合并方案' : '' });
      if (terminal) callbacks.current.onChanged?.(result);
    } catch (reason) {
      if (gate.isCurrent(token) && isDeviceIdentity(identity)) {
        try { markMergeCommand({ ...selected, phase: 'unknown' }); }
        catch { reason = new Error('合并操作记录未核对 · 保留浏览器资料'); }
        setState(old => ({ ...old, operation: null, error: reason.message, busy: false }));
      }
    } finally { if (pending.current === token) pending.current = null; }
  }
  useEffect(() => {
    if (command && !operation && !busy && !value?.error)
      run(command, () => transport.read(projectId, command.id));
  }, [scope, command?.id]);

  function prepare() {
    if (!eligible || busy || pending.current || commands.length || value?.blocked) return;
    try {
      const selected = beginMergeCommand(storage, projectId, candidate, target);
      setState({ scope, commands: [selected], selected: selected.id, operation: null, error: '', busy: false });
      run(selected, () => transport.prepare({ project_id: projectId, operation_id: selected.id,
        candidate_id: candidate.id, candidate_revision: candidate.revision, target_id: target.id,
        target_revision: target.revision, content: candidate.text, conditions }));
    } catch (reason) { setState(old => ({ ...old, error: reason.message })); setRefresh(value => value + 1); }
  }
  async function review(decision) {
    const proposal = operation?.proposal;
    if (!canWrite || !proposal || proposal.state !== 'pending') return;
    run(command, async () => {
      if (shared) return transport.review(command.id, proposal.revision, decision);
      await recognitionApi.reviewRestructureProposal({ projectId, proposalId: proposal.id,
        expectedRevision: proposal.revision, decision });
      return transport.read(projectId, command.id);
    });
  }
  if (!duplicate && !manual && !commands.length && !value?.error && !value?.notice) return null;
  return <section className="library-grown" aria-label="审核合并">
    {value?.error && <p role="alert">{value.error}</p>}
    {value?.notice && <p role="status">{value.notice}</p>}
    {busy && <p role="status">核对合并状态</p>}
    {command && <p className="library-meta">本次合并 · {command.body.candidate_id} · {command.body.target_id}</p>}
    {commands.length > 1 && <label className="library-queue-target">待核对操作<select disabled={busy} value={command?.id || ''} onChange={event => {
      gate.invalidate('operation'); setState(old => ({ ...old, selected: event.target.value, operation: null, error: '' }));
    }}>{commands.map((row, index) => <option key={row.id} value={row.id}>操作 {index + 1}</option>)}</select></label>}
    {command && !operation && <div className="library-actions">
      <button type="button" disabled={busy} onClick={() => run(command, () => transport.read(projectId, command.id))}>重新读取合并</button>
      <button type="button" disabled={busy || !canWrite} onClick={() => run(command, () => transport.cancel(projectId, command.id))}>取消未受理操作</button>
    </div>}
    {operation?.proposal && <>
      <h3>合并后待确认</h3>
      {operation.proposal.outputs.map((output, index) => <div key={index}><p className="library-summary-text">{output.content}</p>
        {output.conditions.length > 0 && <ul>{output.conditions.map(condition => <li key={condition}>{condition}</li>)}</ul>}</div>)}
      <div className="library-actions"><button type="button" disabled={busy || !canWrite} onClick={() => review('approve')}>确认合并方案</button>
        <button type="button" disabled={busy || !canWrite} onClick={() => review('reject')}>忽略合并方案</button>
        <button type="button" disabled={busy} onClick={() => run(command, () => transport.read(projectId, command.id))}>重新读取合并</button></div>
    </>}
    {!command && manual && <label className="library-queue-target">与已有认识合并
      <select value={selectedTarget} disabled={busy || !targets} onChange={event => setSelectedTarget(event.target.value)}>
        <option value="">{targets ? '请选择' : '读取已有认识'}</option>
        {targets?.map(row => <option key={row.id} value={row.id}>{row.text.slice(0, 100)}</option>)}
      </select>
    </label>}
    {!command && targetError && <p role="alert">{targetError} <button type="button" onClick={() => setTargetRefresh(value => value + 1)}>重新读取</button></p>}
    {!command && duplicate && <>
      <h3>{shared ? '原认识' : '与已有认识重复'}</h3><p className="library-summary-text">{target.text}</p>
      {onOpenTarget && typeof target.project_id === 'string' && target.project_id && <button type="button" disabled={busy} onClick={() => onOpenTarget(target, target.project_id)}>打开已有认识</button>}
      {eligible ? <details><summary>合并预览</summary><p className="library-summary-text">{candidate.text}</p>
        {conditions.length > 0 && <ul>{conditions.map(condition => <li key={condition}>{condition}</li>)}</ul>}
        <p className="library-meta">合并后仍待确认 · 可先编辑当前正文</p>
        <button type="button" disabled={busy || value?.blocked} onClick={prepare}>保存合并预览</button>
      </details> : <p className="library-meta">合并需要同一项目的可编辑认识</p>}
    </>}
  </section>;
}
