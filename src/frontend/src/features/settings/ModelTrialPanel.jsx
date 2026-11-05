import { useEffect, useRef, useState } from 'react';
import { Row } from '../../shared/ui';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { formatModelCost } from '../../shared/lib/modelCost';
import { providerConnectionsApi } from './providerConnectionsApi';
import { useProviderOperations } from './useProviderOperations';
import { beginProviderOperation, markProviderOperation, finishProviderOperation } from './providerOperationJournal';
import './ProviderConnections.css';

const revisionOf = model => ({ generation: model.generation.revision, mode: model.generation_mode.revision, fast: model.generation.fast_model?.revision || 0 });
const sameRevision = (left, right) => Object.keys(left).every(key => left[key] === right[key]);
const explanations = { provider_trial_identity_changed: '身份已变化 · 重新登录', provider_trial_fast_unconfigured: '快模型未配置 · 先配置模型', provider_trial_configuration_changed: '模型设置已变化 · 核对后再试用', provider_trial_generation_disabled: '先启用生成用途', provider_trial_remote_disabled: '检查当前项目私密与生成外发', model_egress_remote_not_consented: '先开启生成外发', server_model_balance_insufficient: '共享模型余额不足', server_model_configuration_readonly: '共享模型仅使用已分配配置', provider_connection_changed: '连接已变化 · 核对后再试用', provider_trial_failed: '试用未完成 · 查看当前模型配置' };

function useModelTrial(projectId, model, api, onConflict) {
  const journal = useProviderOperations(), immediateBusy = useRef(false), commandRef = useRef(null), newIntent = useRef(false);
  const gate = useRequestScope(`${journal.scope}:${projectId}`), scope = gate.scope;
  const [working, setWorking] = useState(false), [pending, setPending] = useState(null), [value, setValue] = useState(null), [error, setError] = useState('');
  const reconciled = useRef(new Set());
  useEffect(() => { if (pending && commandRef.current?.id === pending && journal.ready && !working && !journal.commands.some(command => command.id === pending) && !reconciled.current.has(pending)) { reconciled.current.add(pending); read(); } }, [pending, journal.commands, journal.ready, working]);
  const activeScope = useRef(scope), visible = activeScope.current === scope;
  useEffect(() => { if (activeScope.current !== scope) { activeScope.current = scope; immediateBusy.current = false; commandRef.current = null; newIntent.current = false; setWorking(false); setPending(null); setValue(null); setError(''); } }, [scope]);
  function accept(result) { if (!['complete', 'failed', 'cancelled', 'running', 'unknown', 'not_recorded'].includes(result?.status) || !Array.isArray(result.results)) throw new Error('invalid_trial_result'); setValue(result); if (['complete', 'failed', 'cancelled'].includes(result.status)) { if (commandRef.current) finishProviderOperation(commandRef.current); setPending(null); } else if (commandRef.current) markProviderOperation(commandRef.current, result.status === 'not_recorded' ? 'not_received' : 'pending'); }
  async function start({ text, targets, draft = null, revision = revisionOf(model) }) {
    if (immediateBusy.current || working || pending) return;
    const token = gate.issue('trial', scope); if (!token) return;
    const id = `trial-${crypto.randomUUID()}`;
    immediateBusy.current = true;
    setWorking(true); setError(''); setValue(null); setPending(id);
    try {
      commandRef.current = beginProviderOperation(journal.scope, { id, kind: 'trial', target: id, projectId: projectId || 'default' }, { newTrial: newIntent.current });
      newIntent.current = false; markProviderOperation(commandRef.current, 'sending');
      const result = await api.trial({ trial_id: id, project_id: projectId || 'default', text, targets, draft,
        expected_generation_revision: revision.generation, expected_mode_revision: revision.mode, expected_fast_revision: revision.fast });
      if (gate.isCurrent(token)) accept(result);
    } catch (reason) {
      if (!gate.isCurrent(token)) return;
      if (!commandRef.current || commandRef.current.id !== id) { setPending(null); setError(reason.message || '操作记录未保存 · 输入已保留'); journal.refresh(); return; }
      if (reason.status && reason.status < 500 && reason.detail !== 'provider_trial_already_recorded') {
        let cancelled;
        try { cancelled = await api.cancelTrial(id); accept(cancelled); }
        catch { journal.refresh(); }
        setError(cancelled && cancelled.status !== 'cancelled' ? '本次已受理 · 继续读取结果' : explanations[reason.detail] || '试用未开始 · 输入已保留');
        if (reason.status === 409) onConflict?.();
      } else { try { markProviderOperation(commandRef.current, 'unknown'); } catch { journal.refresh(); } setError('本次结果未知 · 读取同一次试用'); }
    } finally { if (gate.isCurrent(token)) { immediateBusy.current = false; setWorking(false); } }
  }
  async function read(cancel = false) {
    if (immediateBusy.current || !pending || commandRef.current?.id !== pending) return;
    const token = gate.issue('trial', scope); if (!token) return;
    immediateBusy.current = true;
    setWorking(true); setError('');
    try { const result = await api[cancel ? 'cancelTrial' : 'trialStatus'](pending); if (gate.isCurrent(token)) accept(result); }
    catch { if (gate.isCurrent(token)) setError('结果未读取 · 继续核对'); }
    finally { if (gate.isCurrent(token)) { immediateBusy.current = false; setWorking(false); } }
  }
  const outstanding = journal.commands.filter(command => command.kind === 'trial');
  const canNew = outstanding.length > 0 && outstanding.every(command => command.phase === 'pending');
  function newTrial() { if (!canNew || working || immediateBusy.current) return; newIntent.current = true; commandRef.current = null; setPending(null); setValue(null); setError('新试用 · 原记录继续核对'); }
  return { start, read, working: visible && working, pending: visible ? pending : null, value: visible ? value : null, error: journal.error || (visible ? error : ''), ready: visible && journal.ready, canNew: visible && canNew, newTrial };
}

export function TrialResults({ trial }) {
  return <>
    {trial.error && <p role="alert" className="settings-login-hint">{trial.error}</p>}
    {trial.pending && <div className="settings-provider-actions"><span role="status">{({ running: '试用处理中', unknown: '本次结果待核对', not_recorded: '尚未读取到受理记录' })[trial.value?.status] || '等待本次结果'}</span><button type="button" disabled={trial.working} onClick={() => trial.read()}>读取结果</button>{trial.value?.status === 'not_recorded' && <button type="button" disabled={trial.working} onClick={() => trial.read(true)}>取消试用</button>}</div>}
    {trial.canNew && <div className="settings-provider-actions"><button type="button" disabled={trial.working} title="保留原试用记录，明确新建另一轮试用" onClick={trial.newTrial}>新试用</button></div>}
    <div className={`settings-trial-results ${(trial.value?.results?.length || 0) > 1 ? 'is-comparison' : ''}`}>{(trial.value?.results || []).map(result => <article key={result.target}><Row readOnly title={`${{ main: '主模型', fast: '快模型', draft: '连接测试' }[result.target]} · ${result.model || '—'}`} meta={result.status === 'complete' ? '已完成' : '未完成'}/>{result.text && <p>{result.text}</p>}{result.error && <p role="status">{explanations[result.error] || '试用未完成 · 检查配置与授权'}</p>}<div className="settings-trial-meta"><span>{result.duration_ms ?? '—'} ms</span><span>入 {result.usage?.input_tokens ?? '—'} / 出 {result.usage?.output_tokens ?? '—'}{result.usage?.observed_only ? ' · 部分已知' : ''}</span><span>{result.cost ? formatModelCost(result.cost) : '费用未知'}</span><span>{result.attempts} 次尝试</span></div></article>)}</div>
  </>;
}

export function ModelTrialPanel({ projectId, model, run, busy, api = providerConnectionsApi }) {
  const [text, setText] = useState(''), [target, setTarget] = useState(model.generation.fast_model?.configured ? 'both' : 'main'), [baseline, setBaseline] = useState(revisionOf(model)), [conflict, setConflict] = useState(false);
  const edited = useRef(false);
  useEffect(() => { if (!edited.current) setBaseline(revisionOf(model)); }, [model.generation.revision, model.generation_mode.revision, model.generation.fast_model?.revision]);
  const changed = conflict || !sameRevision(baseline, revisionOf(model));
  const missingFast = target !== 'main' && !model.generation.fast_model?.configured;
  const trial = useModelTrial(projectId, model, api, () => { setConflict(true); run(async () => {}); });
  const locked = busy || trial.working || Boolean(trial.pending) || !trial.ready;
  return <><h2 className="settings-caption">试一句</h2><section className="settings-card settings-model-trial"><form onSubmit={event => { event.preventDefault(); if (!locked && !changed && !missingFast && text.trim()) trial.start({ text: text.trim(), targets: target === 'both' ? ['main', 'fast'] : [target], revision: baseline }); }}>
    <label title="仅发送此处输入，不读取资料库；沿当前项目私密与生成外发设置">试用文字<textarea aria-label="模型试用文字" maxLength={2000} value={text} disabled={locked} onChange={event => { edited.current = true; setText(event.target.value); }}/></label>
    <div className="settings-provider-actions" role="group" aria-label="试用模型">{[['main', '主模型'], ['fast', '快模型'], ['both', '对比']].map(([value, name]) => <button type="button" key={value} aria-pressed={target === value} title={value !== 'main' && !model.generation.fast_model?.configured ? '先在生成用途中配置快模型' : undefined} disabled={locked || value !== 'main' && !model.generation.fast_model?.configured} onClick={() => setTarget(value)}>{name}</button>)}<button type="submit" disabled={locked || changed || missingFast || !text.trim()}>发送</button></div>
    <p className="settings-login-hint">主 {model.generation.model || '未配置'} · 快 {model.generation.fast_model?.configured ? model.generation.fast_model.model : '未配置'}</p>
    {changed && <div className="settings-provider-revision"><p>当前模型设置已变化 · 试用文字保留</p><button type="button" disabled={locked} onClick={() => { setBaseline(revisionOf(model)); setConflict(false); }}>核对模型</button></div>}
  </form><TrialResults trial={trial}/></section></>;
}

export function DraftConnectionTest({ projectId, model, draft, disabled, api = providerConnectionsApi }) {
  const trial = useModelTrial(projectId, model, api);
  const shared = model.generation.server_managed;
  return <div className="settings-connection-test"><div className="settings-provider-actions"><button type="button" title="测试当前表单配置，无需先保存；沿生成用途的启用、外发和当前项目私密设置" disabled={disabled || shared || !trial.ready || trial.working || Boolean(trial.pending) || model.generation_mode.mode !== 'api'} onClick={() => trial.start({ text: 'Reply only with OK.', targets: ['draft'], draft })}>测试连接</button><span className="settings-login-hint" title={shared ? '共享模型只试用管理员实际分配的配置' : '每次请求沿原模型网关、用量与费用记录'}>{shared ? '共享配置' : !model.generation.enabled ? '生成未启用' : !model.generation.allow_remote ? '外发未开' : '按生成用途'}</span></div><TrialResults trial={trial}/></div>;
}
