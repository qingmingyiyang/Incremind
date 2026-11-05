import { useEffect, useRef, useState } from 'react';
import { Row } from '../../shared/ui';
import { userStorageKey } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { clearSharedCommand, readSharedCommands, saveSharedCommand } from '../shared-projects/sharedProjectCommands';
import { formatReceiptCost, formatReceiptUsage } from './ReceiptInspection';

const labels = { vision: '识图', asr: '转写', search: '搜索' };
const terminal = new Set(['complete', 'failed', 'cancelled']);
const statusText = { complete: '已完成实际测试', failed: '测试未完成', cancelled: '未受理并已取消', running: '测试处理中', unknown: '测试结果待核对', not_recorded: '尚未读取到受理记录' };
const messages = { purpose_test_configuration_changed: '设置已变化 · 重新读取样本条件', purpose_test_remote_disabled: '当前项目私密或用途外发未开', purpose_test_unconfigured: '用途尚未配置', purpose_test_asr_disabled: '先开启转写外发', purpose_test_asr_key_unavailable: '先保存转写密钥', purpose_test_asr_consent_required: '先开启转写外发', purpose_test_local_unavailable: '本机识别缺少运行条件', server_model_balance_insufficient: '共享模型余额不足', server_model_purpose_unavailable: '尚未分配此用途', server_model_metering_price_unavailable: '管理员尚未配置用途费率', server_asr_protocol_unavailable: '分配的转写协议不可用', purpose_test_cleanup_unavailable: '临时样本未清理 · 清理后读取结果', purpose_test_failed: '测试未完成 · 检查用途配置与运行条件' };

export function PurposeTest({ purpose, projectId, row, api, disabled }) {
  const space = () => userStorageKey('purpose-tests') + `:${purpose}:${projectId}`;
  const [userSpace, setUserSpace] = useState(space), gate = useRequestScope(`${userSpace}:${purpose}:${projectId}`), scope = gate.scope;
  const [sample, setSample] = useState(null), [commands, setCommands] = useState([]), [results, setResults] = useState({});
  const [selected, setSelected] = useState(null), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const [ready, setReady] = useState(false), [epoch, setEpoch] = useState(0), [newIntent, setNewIntent] = useState(false);
  const immediate = useRef(false), controller = useRef(null);
  const current = token => space() === userSpace && gate.isCurrent(token);
  function pending() {
    const values = readSharedCommands(userSpace);
    if (values.some(command => command.path !== purpose || command.body.id !== command.id || command.body.purpose !== purpose
      || command.body.project_id !== projectId || Object.keys(command.body).some(key => !['id', 'purpose', 'project_id', 'sample_id'].includes(key)))) throw new Error('测试记录无法核对 · 保留浏览器资料');
    setCommands(values); setReady(true); return values;
  }
  function accept(command, value) {
    if (value?.id !== command.id || !Object.hasOwn(statusText, value.status)) throw new Error('测试状态无法核对');
    setResults(values => ({ ...values, [command.id]: value }));
    if (terminal.has(value.status)) clearSharedCommand(command);
    else saveSharedCommand({ ...command, phase: value.status === 'not_recorded' ? 'not_received' : 'pending' });
    pending();
  }
  useEffect(() => {
    setUserSpace(space());
    const changed = () => setUserSpace(space());
    window.addEventListener('chriptmas-user-space', changed); window.addEventListener('chriptmas-device-required', changed);
    return () => { window.removeEventListener('chriptmas-user-space', changed); window.removeEventListener('chriptmas-device-required', changed); };
  }, [purpose, projectId]);
  useEffect(() => {
    setResults({}); setSelected(null); setError(''); setReady(false); setBusy(false); setNewIntent(false); immediate.current = false;
    const request = new AbortController(), token = gate.issue('restore', scope);
    try {
      const values = pending();
      Promise.all(values.map(async command => {
        try { const value = await api.purposeTestStatus(command.id, request.signal); if (current(token)) accept(command, value); }
        catch { if (current(token)) setError('原测试结果未读取 · 按编号继续核对'); }
      }));
    } catch (reason) { setError(reason.message); }
    const stored = event => { if (!event.key || event.key.startsWith(userSpace + ':operation:')) { try { pending(); } catch (reason) { setReady(false); setError(reason.message); } } };
    window.addEventListener('storage', stored);
    return () => { request.abort(); controller.current?.abort(); gate.invalidate('restore'); gate.invalidate('operation'); window.removeEventListener('storage', stored); };
  }, [scope]);
  useEffect(() => {
    const token = gate.issue('sample', scope), request = new AbortController(); setSample(null);
    api.purposeTestSample(purpose, request.signal).then(value => {
      if (!value || value.purpose !== purpose || typeof value.id !== 'string' || !value.expected) throw new Error('sample_invalid');
      if (current(token)) setSample(value);
    }).catch(reason => { if (current(token)) setError(messages[reason.detail] || '测试条件未读取 · 重试'); });
    return () => { request.abort(); gate.invalidate('sample'); };
  }, [scope, epoch, row.revision, row.mode_revision, row.settings_revision, row.secret_generation]);
  async function resolve(command, cancel = false, cleanup = false) {
    if (busy || immediate.current) return;
    const token = gate.issue('operation', scope); if (!token || !current(token)) return;
    immediate.current = true; setBusy(true); setError(''); setSelected(command.id);
    const request = new AbortController(); controller.current = request;
    try { const value = await api[cleanup ? 'cleanupPurposeTest' : cancel ? 'cancelPurposeTest' : 'purposeTestStatus'](command.id, request.signal); if (current(token)) accept(command, value); }
    catch { if (current(token)) setError('本次测试未读取 · 继续核对原编号'); }
    finally { if (current(token)) { immediate.current = false; setBusy(false); } }
  }
  async function start() {
    if (disabled || busy || immediate.current || !ready || !sample || commands.length && !newIntent) return;
    const token = gate.issue('operation', scope); if (!token || !current(token)) return;
    immediate.current = true; setBusy(true); setError(''); setNewIntent(false);
    const identity = `test-${crypto.randomUUID()}`, command = { version: 1, storageKey: userSpace, id: identity, path: purpose,
      body: { id: identity, purpose, project_id: projectId, sample_id: sample.id }, expected: sample.expected,
      phase: 'prepared', createdAt: new Date().toISOString() };
    const request = new AbortController(); controller.current = request; let recorded = false;
    try {
      saveSharedCommand(command); recorded = true; pending(); setSelected(identity); saveSharedCommand({ ...command, phase: 'sending' });
      const value = await api.runPurposeTest({ ...command.body, expected: command.expected }, request.signal);
      if (current(token)) accept(command, value);
    } catch (reason) {
      if (!current(token)) return;
      if (recorded) { try { saveSharedCommand({ ...command, phase: 'unknown' }); pending(); } catch { setReady(false); } }
      setError(recorded ? '本次结果未知 · 读取原测试编号' : '操作记录未保存 · 测试未发送');
    } finally { if (current(token)) { immediate.current = false; setBusy(false); } }
  }
  const value = selected ? results[selected] : null;
  const canNew = commands.length > 0 && commands.every(command => ['unknown', 'running'].includes(results[command.id]?.status));
  if (space() !== userSpace) return null;
  return <section className="settings-connection-test" aria-label={`${labels[purpose]}用途测试`} aria-busy={busy}>
    <div className="settings-test"><button type="button" disabled={disabled || busy || !ready || !sample || commands.length > 0 && !newIntent} onClick={start}>测试{labels[purpose]}</button>
      <span>{sample?.label || '读取测试条件'}</span><button type="button" disabled={busy} onClick={() => { setError(''); setEpoch(value => value + 1); }}>读取条件</button></div>
    <p className="settings-login-hint">已保存配置 · 内置样本 · 沿当前私密、外发与金额规则</p>
    {commands.map(command => <Row key={command.id} readOnly title={statusText[results[command.id]?.status] || '本次测试待核对'} sub={command.id}
      trailing={<><button type="button" disabled={busy} onClick={() => resolve(command)}>读取结果</button>
        {results[command.id]?.status === 'not_recorded' && <button type="button" disabled={busy} onClick={() => resolve(command, true)}>取消未受理测试</button>}
        {results[command.id]?.cleanup === 'failed' && <button type="button" disabled={busy} onClick={() => resolve(command, false, true)}>清理临时样本</button>}</>}/>) }
    {canNew && <button type="button" disabled={busy} onClick={() => { setNewIntent(true); setSelected(null); }}>另做一次测试</button>}
    {value && <article><Row readOnly title={statusText[value.status]} sub={value.id}/>
      {value.text && <p>{value.text}</p>}{value.description && <p>{value.description}</p>}
      {Number.isInteger(value.result_count) && <p>实际搜索 {value.result_count} 条{value.titles?.length > 0 ? ` · ${value.titles.join(' · ')}` : ''}</p>}
      {value.error && <p role="status" title={value.error}>{messages[value.error] || '测试未完成 · 查看用途配置与运行条件'}</p>}
      {Number.isFinite(value.reported_total_tokens) && <p>实际报告总词元 {value.reported_total_tokens}</p>}
      {value.status !== 'cancelled' && <div className="settings-trial-meta"><span>{value.duration_ms ?? '—'} ms</span><span>入 / 出 {formatReceiptUsage(value.usage)}</span>
        <span>{formatReceiptCost(value.cost)}</span><span>已登记 {value.attempt_count ?? '—'} 次尝试</span></div>}
    </article>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
