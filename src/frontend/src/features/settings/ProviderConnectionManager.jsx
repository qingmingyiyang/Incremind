import { useEffect, useRef, useState } from 'react';
import { Row, StatusDot } from '../../shared/ui';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { apiEndpointError } from './modelEndpoint';
import { providerConnectionsApi } from './providerConnectionsApi';
import { DraftConnectionTest } from './ModelTrialPanel';
import { ProviderOperationRecovery } from './ProviderOperationRecovery';
import { useProviderOperations } from './useProviderOperations';
import { beginProviderOperation, markProviderOperation, finishProviderOperation } from './providerOperationJournal';
import './ProviderConnections.css';

const purposeNames = { generation: '生成', vision: '识图', embedding: '向量', rerank: '重排', search: '搜索' };
const operationId = () => `operation-${crypto.randomUUID()}`;
const connectionId = () => `connection-${crypto.randomUUID()}`;

export function ProviderConnectionManager({ projectId, model, presets, run, busy, target, onConfigure, api = providerConnectionsApi }) {
  const gate = useRequestScope(projectId), scope = gate.scope;
  const [items, setItems] = useState(null), [selected, setSelected] = useState(null), [error, setError] = useState(''), [reading, setReading] = useState(false);
  const initialRead = useRef(true);
  async function reload() {
    const token = gate.issue('list', scope); if (!token) return;
    setReading(true); setError('');
    try {
      const result = await api.list();
      if (!Array.isArray(result?.items) || result.items.some(item => typeof item.id !== 'string' || !Number.isInteger(item.revision) || typeof item.name !== 'string' || typeof item.base_url !== 'string' || !Array.isArray(item.models))) throw new Error('invalid_connections');
      if (gate.isCurrent(token)) { setItems(result.items); if (initialRead.current) { initialRead.current = false; setSelected(old => old || result.items.find(item => item.id === model.generation.provider_connection?.id)?.id || result.items[0]?.id || null); } return result.items; }
    } catch { if (gate.isCurrent(token)) setError('连接未读取 · 重新读取'); }
    finally { if (gate.isCurrent(token)) setReading(false); }
  }
  useEffect(() => { reload(); }, [projectId, api]);
  const saved = items?.find(item => item.id === selected);
  const preset = presets.find(item => `new-${item.id}` === selected) || presets[0];
  const special = [
    { id: 'subscription', name: 'ChatGPT 订阅', mark: 'GP', target: 'generation' },
    { id: 'asr', name: 'Hy-ASR', mark: 'HY', target: 'asr' },
    { id: 'local', name: '本机', mark: '本', target: 'embedding' },
  ];
  return <section className="settings-providers" aria-label="服务商连接"><h2 className="settings-caption">服务商<span>{items ? `${items.filter(item => item.enabled && item.has_api_key).length} 个已保存` : '—'}</span></h2>
    <ProviderOperationRecovery api={api} connectionNames={Object.fromEntries((items || []).map(item => [item.id, item.name]))} onResolved={async () => { await reload(); await run(async () => {}); }}/>
    {error && <p role="alert">{error}<button type="button" disabled={reading} onClick={reload}>重新读取</button></p>}
    <div className="settings-provider-grid" role="group" aria-label="选择服务商连接">
      {(items || []).map(item => <button type="button" key={item.id} className="settings-provider-card" aria-pressed={selected === item.id} onClick={() => setSelected(item.id)}><span className="settings-provider-mark">API</span><strong>{item.name}</strong><span><StatusDot state={item.enabled && item.has_api_key ? 'done' : 'unverified'}/>{item.enabled && item.has_api_key ? '已保存' : '已断开'}</span></button>)}
      {presets.map(item => <button type="button" key={item.id} className="settings-provider-card" aria-pressed={!saved && preset.id === item.id} onClick={() => setSelected(`new-${item.id}`)}><span className="settings-provider-mark">{item.mark}</span><strong>{item.name}</strong><span>新增连接</span></button>)}
      {special.map(item => <button type="button" key={item.id} className="settings-provider-card" onClick={() => onConfigure(item.target, item.id)}><span className="settings-provider-mark">{item.mark}</span><strong>{item.name}</strong><span>查看设置</span></button>)}
    </div>
    {items && <ConnectionEditor key={saved?.id || `new-${preset.id}`} connection={saved} preset={preset} model={model} projectId={projectId} api={api} run={run} target={target} disabled={busy || reading} onReload={reload} onSaved={id => setSelected(id)}/>}
  </section>;
}

function ConnectionEditor({ connection, preset, model, projectId, api, run, target, disabled, onReload, onSaved }) {
  const journal = useProviderOperations(), immediateBusy = useRef(false);
  const gate = useRequestScope(`${projectId}:${connection?.id || preset.id}`), scope = gate.scope;
  const id = useRef(connection?.id || connectionId());
  const [name, setName] = useState(connection?.name || preset.name), [base, setBase] = useState(connection?.base_url ?? preset.baseUrl), [models, setModels] = useState(connection?.models?.join('\n') ?? preset.model), [secret, setSecret] = useState('');
  const [baseline, setBaseline] = useState(connection?.revision || 0), [conflict, setConflict] = useState(false), [working, setWorking] = useState(false), [message, setMessage] = useState(''), [pending, setPending] = useState(null);
  const [purpose, setPurpose] = useState('generation'), [selectedModel, setSelectedModel] = useState(connection?.models?.[0] || preset.model);
  const reconciled = useRef(new Set());
  useEffect(() => { if (pending?.command && journal.ready && !working && !immediateBusy.current && !journal.commands.some(command => command.id === pending.operationId) && !reconciled.current.has(pending.operationId)) { reconciled.current.add(pending.operationId); reconcile(); } }, [pending?.operationId, journal.commands, journal.ready, working]);
  const selectionEdited = useRef(false);
  const [selectionBaseline, setSelectionBaseline] = useState({ revision: model.generation?.revision || 0, mode: model.generation_mode.revision, connection: connection?.revision });
  const [selectionConflict, setSelectionConflict] = useState(false);
  const endpointError = apiEndpointError(base);
  const changed = conflict || baseline !== (connection?.revision || 0);
  const locked = disabled || working || Boolean(pending) || !journal.ready;
  const availableModels = models.split('\n').map(value => value.trim()).filter(Boolean);
  const draftChanged = !connection || name.trim() !== connection.name || base.trim() !== connection.base_url || availableModels.join('\n') !== connection.models.join('\n') || Boolean(secret);
  const retain = connection?.has_api_key && connection.base_url === base.trim();
  const selection = model[purpose] || {};
  const selectionChanged = selectionConflict || selectionBaseline.revision !== (selection.revision || 0) || selectionBaseline.mode !== model.generation_mode.revision || selectionBaseline.connection !== connection?.revision;
  function reviewSelection(nextPurpose = purpose) { setSelectionBaseline({ revision: model[nextPurpose]?.revision || 0, mode: model.generation_mode.revision, connection: connection?.revision }); setSelectionConflict(false); }
  useEffect(() => {
    if (!target || !Object.hasOwn(purposeNames, target.purpose)) return;
    selectionEdited.current = false;
    setPurpose(target.purpose); reviewSelection(target.purpose);
  }, [target]);
  useEffect(() => { if (!selectionEdited.current) reviewSelection(); }, [selection.revision, model.generation_mode.revision, connection?.revision]);
  const active = selection.provider_connection?.id === connection?.id && selection.provider_connection?.available;
  const errors = { provider_connection_new_key_required: '地址已变化 · 填入此服务商的新密钥', provider_connection_disconnected: '连接已断开 · 重新保存', select_api_before_provider_connection: '在生成用途中选择 API 后采用', server_model_configuration_readonly: '服务器共享配置 · 由管理员分配' };
  async function reconcile(cancel = false) {
    const token = gate.issue('read', scope); if (!token) return;
    setWorking(true);
    try {
      const proof = await api[cancel ? 'cancelOperation' : 'operation'](pending.operationId);
      const rows = await onReload();
      if (!gate.isCurrent(token)) return;
      const current = rows?.find(row => row.id === id.current);
      if (proof.status === 'accepted') {
        if (proof.connection_id !== id.current) throw new Error('operation_target_changed');
        if (pending.command) finishProviderOperation(pending.command);
        setPending(null);
        if (proof.action === 'selection') setMessage(`本次用途选择已受理 · 当前${purposeNames[proof.purpose]} ${model[proof.purpose]?.model || '未配置'}`);
        else {
          setSecret('');
          if (current) { setBaseline(current.revision); setConflict(current.revision !== proof.result_revision); onSaved(current.id); }
          setMessage(!current ? '本次操作已受理 · 当前连接待读取' : current.revision === proof.result_revision ? '本次操作已完成' : '本次操作已受理 · 随后有新的修改');
        }
        await run(async () => {});
      } else if (proof.status === 'cancelled') { if (pending.command) finishProviderOperation(pending.command); setPending(null); setMessage('本次操作已取消 · 草稿保留'); }
      else setMessage('本次结果尚未核实 · 可继续回读或取消本次操作');
    } catch { if (gate.isCurrent(token)) setMessage('结果未读取 · 继续核对'); }
    finally { if (gate.isCurrent(token)) setWorking(false); }
  }
  async function mutate(action, body) {
    if (immediateBusy.current) return;
    const token = gate.issue('write', scope); if (!token) return;
    immediateBusy.current = true;
    setWorking(true); setMessage('');
    let command, received = false;
    try {
      command = beginProviderOperation(journal.scope, { id: body.operation_id, kind: action, target: id.current });
      markProviderOperation(command, 'sending');
      const result = await api[action](id.current, body);
      if (result?.id !== id.current || !Number.isInteger(result.revision) || typeof result.enabled !== 'boolean') throw new Error('invalid_connection_result');
      if (!gate.isCurrent(token)) return;
      received = true; setSecret(''); finishProviderOperation(command);
      setPending(null); setBaseline(result.revision); setConflict(false); setSecret('');
      setMessage(action === 'save' ? '已保存 · 连通性待测试' : '已断开');
      await onReload(); if (gate.isCurrent(token)) onSaved(result.id);
      await run(async () => {});
    } catch (reason) {
      if (!gate.isCurrent(token)) return;
      if (!command) { setMessage(reason.message || '操作记录未保存 · 输入已保留'); journal.refresh(); return; }
      setPending({ operationId: body.operation_id, command });
      if (reason.status && reason.status < 500 && reason.detail !== 'provider_connection_operation_already_recorded') {
        try { const proof = await api.cancelOperation(command.id); if (proof.status === 'cancelled') { finishProviderOperation(command); setPending(null); if (reason.status === 409) { setConflict(true); await onReload(); } } } catch { journal.refresh(); }
        setMessage(errors[reason.detail] || (reason.status === 409 ? '连接已变化 · 核对当前输入' : '操作未完成 · 输入已保留'));
      } else { try { markProviderOperation(command, 'unknown'); } catch { journal.refresh(); } setMessage(received ? '已受理 · 操作记录待核对' : '操作结果未知 · 先核对本次结果'); }
    } finally { immediateBusy.current = false; if (gate.isCurrent(token)) setWorking(false); }
  }
  async function adopt() {
    if (selectionChanged || immediateBusy.current) return;
    const token = gate.issue('selection', scope); if (!token) return;
    setWorking(true); setMessage('');
    immediateBusy.current = true;
    const operation = operationId();
    let command;
    await run(async () => {
      try {
        command = beginProviderOperation(journal.scope, { id: operation, kind: 'selection', target: connection.id, purpose });
        markProviderOperation(command, 'sending');
        await api.select(connection.id, { purpose, model: selectedModel, operation_id: operation, expected_revision: selectionBaseline.connection,
          expected_configuration_revision: selectionBaseline.revision, expected_mode_revision: selectionBaseline.mode });
        finishProviderOperation(command);
        if (gate.isCurrent(token)) { selectionEdited.current = false; setMessage('用途已采用 · 外发开关保持'); }
      } catch (reason) { if (gate.isCurrent(token)) { setSelectionConflict(true); if (command) { setPending({ operationId: operation, command }); try { markProviderOperation(command, 'unknown'); } catch { journal.refresh(); } } setMessage(command ? errors[reason.detail] || '选择未完成 · 读取用途核对' : reason.message); } throw reason; }
    });
    immediateBusy.current = false;
    if (gate.isCurrent(token)) setWorking(false);
  }
  return <section className="settings-card settings-provider-detail" aria-label="编辑服务商连接"><header><span className="settings-provider-mark">API</span><h3>{connection?.name || preset.name}</h3><span>{connection ? connection.enabled ? '已保存' : '已断开' : '未保存'}</span></header>
    <form className="settings-provider-form" onSubmit={event => { event.preventDefault(); if (locked || changed || endpointError || !name.trim() || !availableModels.length || !secret && !retain) return;
      mutate('save', { name: name.trim(), base_url: base.trim(), models: availableModels, api_key: secret, expected_revision: baseline, operation_id: operationId() }); }}>
      <div className="settings-fields"><label>名称<input value={name} maxLength={80} disabled={locked} onChange={event => setName(event.target.value)}/></label><label>地址<input aria-label="服务商地址" value={base} disabled={locked} aria-invalid={Boolean(endpointError)} onChange={event => setBase(event.target.value)}/></label><label className="settings-provider-wide">模型 · 每行一个<textarea aria-label="连接模型列表" value={models} maxLength={12864} disabled={locked} onChange={event => setModels(event.target.value)}/></label></div>
      {endpointError && <span className="settings-login-hint" role="status">{endpointError}</span>}
      <div className="settings-provider-key"><label>密钥<input type="password" autoComplete="new-password" value={secret} disabled={locked} placeholder={retain ? '已保存 · 留空保留' : '填入此连接密钥'} onChange={event => setSecret(event.target.value)}/></label><button type="submit" disabled={locked || changed || endpointError || !name.trim() || !availableModels.length || !secret && !retain}>保存连接</button></div>
      {changed && <section className="settings-provider-revision"><p>连接已变化 · 当前 {connection?.name || '未保存'}</p><p>{connection?.base_url}</p><button type="button" disabled={locked} onClick={() => { setBaseline(connection?.revision || 0); setConflict(false); setMessage('已核对当前连接 · 草稿保留'); }}>重新核对</button></section>}
    </form>
    <DraftConnectionTest projectId={projectId} model={model} api={api} disabled={locked || changed || Boolean(endpointError) || !availableModels.length || !secret && !retain}
      draft={{ base_url: base.trim(), model: availableModels[0] || '', api_key: secret,
        connection_id: connection?.id || null, expected_connection_revision: connection?.revision || null }}/>
    {pending && <div className="settings-provider-actions"><button type="button" disabled={working || disabled} onClick={() => reconcile()}>回读结果</button><button type="button" disabled={working || disabled} onClick={() => reconcile(true)}>取消操作</button></div>}
    {connection && <><Row readOnly title="用于" sub={draftChanged ? '先保存连接修改，再采用用途' : active ? `${purposeNames[purpose]} · ${selection.model}` : '选择用途后采用，外发单独控制'}/><div className="settings-provider-actions"><select aria-label="连接用途" value={purpose} disabled={locked} onChange={event => { setPurpose(event.target.value); reviewSelection(event.target.value); selectionEdited.current = true; }}>{Object.entries(purposeNames).map(([value, title]) => <option key={value} value={value}>{title}</option>)}</select><select aria-label="采用模型" value={connection.models.includes(selectedModel) ? selectedModel : ''} disabled={locked} onChange={event => { selectionEdited.current = true; setSelectedModel(event.target.value); }}><option value="">选择模型</option>{connection.models.map(value => <option key={value} value={value}>{value}</option>)}</select><button type="button" disabled={locked || draftChanged || selectionChanged || !connection.models.includes(selectedModel) || !connection.enabled || !connection.has_api_key || selection.server_managed || purpose === 'generation' && model.generation_mode.mode !== 'api'} onClick={adopt}>采用</button><button type="button" disabled={locked || !connection.enabled} onClick={() => mutate('disconnect', { expected_revision: connection.revision, operation_id: operationId() })}>断开</button></div>{selectionChanged && <div className="settings-provider-revision"><p>当前{purposeNames[purpose]} · {selection.model || '未配置'}</p><button type="button" disabled={locked} onClick={() => reviewSelection()}>核对用途</button></div>}</>}
    {message && <p className="settings-login-hint" role="status">{message}</p>}
  </section>;
}
