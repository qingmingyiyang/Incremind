import { useEffect, useRef, useState } from 'react';
import { Row } from '../../shared/ui/Row';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { userStorageKey } from '../../shared/api/deviceTransport';
import { routeCorrectionApi } from './routeCorrectionApi';
import * as journal from './routeCorrectionJournal';
import './routeCorrection.css';

const labels = { remember: '记住', ask: '问', do: '干活', inspiration: '灵感' };
const statuses = { completed: '已重做', partial: '部分未完成', running: '正在重做', unknown: '待核对', cancelled: '已取消', not_received: '未读取到受理' };
export function RouteCorrectionPanel({ turnId, projectId, initialIntent, onCompleted, onOpenReplacement, api = routeCorrectionApi }) {
  const storageScope = userStorageKey('route-corrections-v1');
  const scopeKey = `${storageScope}:${projectId}:${turnId}`, gate = useRequestScope(scopeKey);
  const [state, setState] = useState({ scope: scopeKey, source: null, parts: [], pending: [], results: {}, error: '', busy: false });
  const current = state.scope === scopeKey ? state : { source: null, parts: [], pending: [], results: {}, error: '', busy: true };
  const lock = useRef(null), liveScope = useRef(scopeKey); liveScope.current = scopeKey;
  const panel = useRef(null), nextFocus = useRef(null);
  function update(changes) { if (liveScope.current === scopeKey) setState(old => ({ ...(old.scope === scopeKey ? old : { scope: scopeKey, results: {} }), ...changes })); }
  function pending() { try { update({ pending: journal.commands(storageScope) }); } catch (error) { update({ error: error.message }); } }
  async function load(preserve = false) {
    const token = gate.issue('load', gate.scope); if (!token) return;
    try {
      const source = await api.read(turnId, projectId);
      if (!gate.isCurrent(token)) return;
      const keepDraft = preserve && current.source && current.source.can_correct !== false && source.can_correct !== false;
      update({ source, ...(!keepDraft ? { parts: source.parts.map((part, index) => ({ sources: [index], intent: source.parts.length === 1 && initialIntent ? initialIntent : part.intent, depends_on: [...part.depends_on], target_source: index })) } : {}), originalResult: null, error: '', busy: false });
    } catch (error) { if (gate.isCurrent(token)) update({ error: error.message, busy: false }); }
  }
  async function readOriginal() {
    const id = current.source?.operation_id;
    if (!id) return;
    const token = gate.issue('original-operation', gate.scope); if (!token) return;
    try {
      const result = await api.operation(id);
      if (gate.isCurrent(token) && result.operation_id === id) update({ originalResult: result, error: '' });
    } catch (error) { if (gate.isCurrent(token)) update({ error: error.message }); }
  }
  async function read(command, cancel = false) {
    const token = gate.issue(command.id, gate.scope); if (!token) return;
    try {
      const proof = await api[cancel ? 'cancel' : 'operation'](command.id);
      if (!gate.isCurrent(token)) return;
      if (proof.operation_id !== command.id || proof.source_turn_id && (proof.source_turn_id !== command.body.turnId || proof.project_id !== command.body.projectId)) throw new Error('纠正身份无法核对');
      setState(old => old.scope === scopeKey ? { ...old, results: { ...old.results, [command.id]: proof } } : old);
      if (['completed', 'partial', 'cancelled'].includes(proof.status)) {
        journal.settle(command);
        if (command.body.turnId === turnId) await load(true);
        if (proof.replacement_turn_id) await onCompleted?.(proof);
      } else journal.mark(command, proof.status === 'not_received' ? 'not_received' : 'pending');
    } catch (error) { if (gate.isCurrent(token)) update({ error: error.message }); }
  }
  useEffect(() => {
    lock.current = null;
    setState({ scope: scopeKey, source: null, parts: [], pending: [], results: {}, error: '', busy: false });
    load(); pending();
    const listener = () => pending();
    window.addEventListener('storage', listener); window.addEventListener(journal.ROUTE_CORRECTION_EVENT, listener);
    return () => { window.removeEventListener('storage', listener); window.removeEventListener(journal.ROUTE_CORRECTION_EVENT, listener); };
  }, [scopeKey]);
  const restored = useRef(new Set());
  useEffect(() => { for (const command of current.pending) if (!restored.current.has(`${storageScope}:${command.id}`)) { restored.current.add(`${storageScope}:${command.id}`); read(command); } }, [current.pending, storageScope]);
  useEffect(() => {
    if (!current.pending.length) return;
    const timer = setTimeout(async () => {
      for (const command of current.pending) await read(command);
      if (liveScope.current === scopeKey) pending();
    }, 2000);
    return () => clearTimeout(timer);
  }, [current.pending, scopeKey]);
  function edit(index, patch) { update({ parts: current.parts.map((part, number) => number === index ? { ...part, ...patch } : part) }); }
  function remove(index) {
    nextFocus.current = Math.max(0, index - 1);
    // 删除时保留缺失依赖提示，由用户在完整表中明确选择新的依赖。
    update({ parts: current.parts.filter((_, number) => number !== index).map(part => ({ ...part,
      depends_on: part.depends_on.map(number => number === index ? -1 : number > index ? number - 1 : number) })) });
  }
  function merge(index) {
    const left = current.parts[index], right = current.parts[index + 1];
    if (!right) return;
    nextFocus.current = index;
    const dependencies = [...new Set([...left.depends_on, ...right.depends_on])].filter(number => number !== index && number !== index + 1).map(number => number > index + 1 ? number - 1 : number);
    update({ parts: current.parts.filter((_, number) => number !== index + 1).map((part, number) => number === index
      ? { ...left, sources: [...left.sources, ...right.sources], depends_on: dependencies }
      : { ...part, depends_on: [...new Set(part.depends_on.map(value => value === index + 1 ? index : value > index + 1 ? value - 1 : value))] } ) });
  }
  useEffect(() => {
    if (nextFocus.current === null) return;
    const controls = panel.current?.querySelectorAll('select');
    (controls?.[nextFocus.current] || panel.current?.querySelector('button'))?.focus();
    nextFocus.current = null;
  }, [current.parts]);
  async function submit() {
    if (lock.current || !current.source) return;
    const token = gate.issue('submit', gate.scope); if (!token) return;
    lock.current = token; update({ busy: true, error: '' });
    let command;
    try {
      command = journal.prepare(storageScope, turnId, projectId); journal.mark(command, 'sending');
      await api.submit(turnId, { project_id: projectId, operation_id: command.id, expected_revision: current.source.revision,
        source_revision: current.source.source_revision, parts: current.parts });
      if (gate.isCurrent(token)) await read(command);
    } catch (error) { if (gate.isCurrent(token)) { update({ error: error.message }); if (command) await read(command); } }
    finally { if (gate.isCurrent(token)) { lock.current = null; pending(); update({ busy: false }); } }
  }
  async function removeExample() {
    if (lock.current || !current.source?.example) return;
    const token = gate.issue('submit', gate.scope); if (!token) return;
    lock.current = token; update({ busy: true, error: '' });
    let command;
    try {
      command = journal.prepare(storageScope, turnId, projectId, 'route-example-delete'); journal.mark(command, 'sending');
      await api.removeExample(turnId, { project_id: projectId, operation_id: command.id, expected_revision: current.source.example.revision });
      if (gate.isCurrent(token)) await read(command);
    } catch (error) { if (gate.isCurrent(token)) { update({ error: error.message }); if (command) await read(command); } }
    finally { if (gate.isCurrent(token)) { lock.current = null; pending(); update({ busy: false }); } }
  }
  const unresolved = current.parts.some(part => part.depends_on.includes(-1));
  const dirty = !!current.source && (current.parts.length !== current.source.parts.length || current.parts.some((part, index) =>
    part.sources.length !== 1 || part.sources[0] !== index || part.intent !== current.source.parts[index]?.intent || current.source.parts[index]?.turn_id === null
    || JSON.stringify(part.depends_on) !== JSON.stringify(current.source.parts[index]?.depends_on)));
  return <section ref={panel} className="route-correction" aria-label="纠正分段">
    {current.source?.can_correct === false && <div><p>原执行仍需核对 · 保留原计划与结果</p>
      {current.source.inspection.map(part => <p key={part.index}>{part.index + 1} · {labels[part.intent]} · {part.turn_id ? '已有结果' : '尚未创建结果'}</p>)}
      {current.source.operation_id && <button type="button" onClick={readOriginal}>读取原操作</button>}
      {current.originalResult && <p role="status">{statuses[current.originalResult.status] || '待核对'}</p>}</div>}
    {current.source?.replacement && <p>已替换 · 原结果保留 {onOpenReplacement && <button type="button" onClick={() => onOpenReplacement(current.source.replacement)}>查看新结果</button>}</p>}
    <fieldset className="route-correction-fields" disabled={current.busy || !!current.source?.replacement || current.source?.can_correct === false}>
    {current.parts.map((part, index) => <div className="route-correction-part" key={part.sources.join(':')}>
      {part.sources.some(source => current.source.parts[source].turn_id === null) && <p>原操作未创建本部分 · 将按新操作重新执行</p>}
      <Row readOnly title={`${index + 1} · ${part.sources.map(source => current.source.parts[source].span).join('\n').trim() || '附件'}`} trailing={<select aria-label={`第${index + 1}部分意图`} value={part.intent} onChange={event => edit(index, { intent: event.target.value })}>{part.intent === 'inspiration' && <option value="inspiration" disabled>灵感</option>}<option value="remember">记住</option><option value="ask" disabled={!part.sources.some(source => current.source.parts[source].span.trim())}>问</option><option value="do" disabled={!part.sources.some(source => current.source.parts[source].span.trim())}>干活</option></select>}/>
      {!part.sources.some(source => current.source.parts[source].span.trim()) && <p>仅有附件 · 可合并或删除</p>}
      <Row readOnly title="执行范围" trailing={<select aria-label={`第${index + 1}部分执行范围`} value={part.target_source} onChange={event => edit(index, { target_source: Number(event.target.value) })}>{part.sources.map(source => { const original = current.source.parts[source], scope = original.scope; return <option key={source} value={source}>{original.scope_label}{scope.scene ? ` / ${scope.scene}` : ''} · 个人空间 · 来源 {source + 1}</option>; })}</select>}/>
      <div className="route-correction-actions"><span>依赖</span>{current.parts.map((other, number) => number !== index && <label key={number}><input type="checkbox" checked={part.depends_on.includes(number)} onChange={event => edit(index, { depends_on: event.target.checked ? [...part.depends_on.filter(value => value >= 0), number].sort((a, b) => a - b) : part.depends_on.filter(value => value !== number && value >= 0) })}/>{number + 1} · {labels[other.intent]}</label>)}{part.depends_on.includes(-1) && <button type="button" onClick={() => edit(index, { depends_on: part.depends_on.filter(value => value >= 0) })}>确认移除缺失依赖</button>}<button type="button" disabled={index + 1 === current.parts.length} onClick={() => merge(index)}>合并下一部分</button><button type="button" onClick={() => remove(index)}>删除部分</button></div>
      {part.sources.some(source => current.source.parts[source].item_ids?.length) && <p>保留已保存附件作为来源 · {part.sources.reduce((total, source) => total + current.source.parts[source].item_ids.length, 0)} 份</p>}
    </div>)}
    {!current.parts.length && current.source && current.source.can_correct !== false && <p>移除全部部分 · 原结果保留</p>}
    </fieldset>
    {current.error && <p role="alert">{current.error}</p>}
    <div className="route-correction-actions"><button type="button" disabled={current.source?.can_correct === false || !dirty || current.busy || unresolved || !!current.source?.replacement || current.pending.some(command => command.body.turnId === turnId)} onClick={submit}>{current.busy ? '正在提交' : '保存并重做'}</button><button type="button" disabled={current.busy} onClick={() => load(true)}>重新核对修订</button></div>
    {current.source?.example && <button type="button" disabled={current.busy || current.source.example.deleted} onClick={removeExample}>{current.source.example.deleted ? '样例已删除' : '删除路由样例'}</button>}
    {current.pending.map(command => <Row readOnly key={command.id} title="纠正待核对" sub={command.body.turnId === turnId ? undefined : '其他对话'} meta={statuses[current.results[command.id]?.status] || '待读取'} trailing={<><button type="button" onClick={() => read(command)}>读取结果</button>{current.results[command.id]?.status === 'not_received' && <button type="button" onClick={() => read(command, true)}>取消未受理操作</button>}</>}/>)}
  </section>;
}
