import { useEffect, useRef, useState } from 'react';
import { Row } from '../../shared/ui';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { useProviderOperations } from './useProviderOperations';
import { finishProviderOperation, markProviderOperation } from './providerOperationJournal';
import { TrialResults } from './ModelTrialPanel';

const labels = { save: '保存连接', disconnect: '断开连接', selection: '采用用途', trial: '模型试用' };

export function ProviderOperationRecovery({ api, onResolved, connectionNames = {} }) {
  const journal = useProviderOperations(), gate = useRequestScope(journal.scope), scope = gate.scope;
  const [savedValues, setSavedValues] = useState({ scope: journal.scope, items: {} }), [busy, setBusy] = useState({}), [errors, setErrors] = useState({});
  const values = savedValues.scope === journal.scope ? savedValues.items : {};
  const readIds = useRef({ scope: journal.scope, ids: new Set() });
  if (readIds.current.scope !== journal.scope) readIds.current = { scope: journal.scope, ids: new Set() };
  async function read(command, cancel = false) {
    const token = gate.issue(command.id, scope); if (!token) return;
    setBusy(old => ({ ...old, [command.id]: true }));
    try {
      const trial = command.kind === 'trial';
      const proof = await api[trial ? cancel ? 'cancelTrial' : 'trialStatus' : cancel ? 'cancelOperation' : 'operation'](command.id);
      if (!gate.isCurrent(token)) return;
      if (trial ? proof.id !== command.id : proof.operation_id !== command.id) throw new Error('操作身份无法核对');
      if (!trial && proof.status === 'accepted' && (proof.connection_id !== command.target || proof.action !== command.kind)) throw new Error('操作目标无法核对');
      setSavedValues(old => ({ scope: journal.scope, items: { ...(old.scope === journal.scope ? old.items : {}), [command.id]: { command, proof } } })); setErrors(old => ({ ...old, [command.id]: '' }));
      if (trial ? ['complete', 'failed', 'cancelled'].includes(proof.status) : ['accepted', 'cancelled'].includes(proof.status)) {
        finishProviderOperation(command); await onResolved?.(command, proof);
      } else markProviderOperation(command, proof.status === 'not_recorded' ? 'not_received' : 'pending');
    } catch (reason) { if (gate.isCurrent(token)) setErrors(old => ({ ...old, [command.id]: reason.message || '结果未读取 · 重试' })); }
    finally { if (gate.isCurrent(token)) setBusy(old => ({ ...old, [command.id]: false })); }
  }
  useEffect(() => { for (const command of journal.commands) if (!readIds.current.ids.has(command.id)) { readIds.current.ids.add(command.id); read(command); } }, [journal.commands, journal.scope]);
  const rows = new Map(journal.commands.map(command => [command.id, { command, proof: values[command.id]?.proof }]));
  for (const [id, value] of Object.entries(values)) if (!rows.has(id) && ['accepted', 'complete', 'failed', 'cancelled'].includes(value.proof?.status)) rows.set(id, value);
  if (!journal.error && !rows.size) return null;
  return <section className="settings-card settings-provider-detail settings-provider-recovery" aria-label="操作核对"><h3>操作核对</h3>{journal.error && <p role="alert">{journal.error}</p>}{[...rows.values()].map(({ command, proof }) => <div key={command.id}><Row readOnly title={labels[command.kind]} sub={command.kind === 'trial' ? proof?.text?.slice(0, 80) : connectionNames[command.target]} meta={({ accepted: '已受理', complete: '已完成', failed: '未完成', cancelled: '已取消', running: '处理中', unknown: '待核对', not_recorded: '未读取到受理' })[proof?.status] || '待读取'} trailing={<><button type="button" disabled={busy[command.id]} onClick={() => read(command)}>读取结果</button>{proof?.status === 'not_recorded' && <button type="button" disabled={busy[command.id]} onClick={() => read(command, true)}>取消操作</button>}</>}/>{errors[command.id] && <p role="alert">{errors[command.id]}</p>}{command.kind === 'trial' && proof && <TrialResults trial={{ value: proof }}/>}</div>)}</section>;
}
