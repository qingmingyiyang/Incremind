import { useEffect, useRef, useState } from 'react';
import { Row } from '../../shared/ui';
import { deviceIdentity, isDeviceIdentity } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { formatModelCost } from '../../shared/lib/modelCost';

const zone = () => Intl.DateTimeFormat().resolvedOptions().timeZone;
function monthAt(timezone) {
  const parts = new Intl.DateTimeFormat('en', { timeZone: timezone, year: 'numeric', month: '2-digit' }).formatToParts(new Date());
  return `${parts.find(part => part.type === 'year').value}-${parts.find(part => part.type === 'month').value}`;
}
const validCount = value => Number.isSafeInteger(value) && value >= 0;
const callStates = { completed: '完成', succeeded: '完成', failed: '失败', failed_transport: '发送失败',
  failed_validation: '核对失败', failed_provider: '调用失败', failed_protocol: '响应未完成', cancelled: '已取消', consumer_cancelled: '已取消', timed_out: '超时', partial: '部分完成' };
const recordStates = { delivered: '已登记交付', recorded: '调用已登记', historical: '历史记录 · 发送状态未记录',
  consented: '已授权 · 发送状态未记录', started: '已受理 · 结果未记录', completed: '领域记录完成', failed: '领域记录失败', cancelled: '已取消' };
function receiptTime(value, timezone) {
  const at = new Date(value);
  return value && Number.isFinite(at.getTime()) ? new Intl.DateTimeFormat('zh-CN', {
    timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(at) : '时间未记录';
}
export function formatReceiptCost(cost) {
  if (!cost || !['known', 'partial'].includes(cost.state) || !Array.isArray(cost.amounts) || !cost.amounts.length) return '—';
  const parts = cost.amounts.map(value => value.currency === 'CNY' ? formatModelCost(value)
    : /^[A-Z]{3}$/.test(value.currency) && /^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/.test(String(value.amount)) ? `${value.currency} ${value.amount}` : '—');
  return `${parts.join(' + ')}${cost.state === 'partial' ? ' · 部分已知' : ''}`;
}
export function formatReceiptUsage(usage) {
  if (!usage || usage.state === 'unknown') return '—';
  return `${usage.state === 'partial' ? '已知 ' : ''}${usage.input ?? '—'} / ${usage.output ?? '—'}`;
}
export function useMonthlyUsage(api, key, scopeMode = 'egress') {
  const [timezone] = useState(zone), [month, setMonth] = useState(() => monthAt(timezone));
  const [data, setData] = useState(null), [error, setError] = useState(false), [epoch, setEpoch] = useState(0);
  const gate = useRequestScope(`${key}:${timezone}:${month}:${scopeMode}`), scope = gate.scope;
  useEffect(() => {
    const timer = setInterval(() => setMonth(monthAt(timezone)), 60000);
    return () => clearInterval(timer);
  }, [timezone]);
  useEffect(() => {
    const token = gate.issue('month', scope), identity = deviceIdentity(), controller = new AbortController();
    setData(null); setError(false);
    Promise.resolve().then(() => api.monthlyUsage({ month, timezone, scope: scopeMode }, controller.signal)).then(value => {
      if (!validCount(value?.receipt_count) || !validCount(value?.model_call_count) || !validCount(value.wire_attempt_count) || !Array.isArray(value.purposes)
          || value.window?.month !== month || value.window?.timezone !== timezone || value.scope !== scopeMode) throw new Error('receipt_summary_invalid');
      if (gate.isCurrent(token) && isDeviceIdentity(identity)) setData(value);
    }).catch(() => { if (gate.isCurrent(token) && isDeviceIdentity(identity)) setError(true); });
    return () => { controller.abort(); gate.invalidate('month'); };
  }, [api, scope, epoch]);
  return { data, error, timezone, month, refresh: () => setEpoch(value => value + 1) };
}

export function ReceiptScopeNote({ summary }) {
  if (!summary) return null;
  const coverage = summary.coverage || {};
  const counts = [['external_incomplete', '未完成'], ['external_unverified', '待核验'], ['undated', '时间未记录'], ['undated_attempts', '尝试时间未记录'], ['unattributed_receipts', '调用归属未记录']]
    .filter(([key]) => coverage[key] > 0).map(([key, label]) => `${coverage[key]} 条${label}`);
  return <p className="settings-support-empty" title={`${summary.window.start} ≤ 时间 < ${summary.window.end}`}>
    {summary.window.month} · {summary.window.timezone}
    {counts.length > 0 && <span role="status"> · 全部记录另有 {counts.join(' · ')}</span>}
    <span> · 模型费用按尝试完成时间统计，进行中未计入</span>
  </p>;
}

function ReceiptDetail({ value, timezone }) {
  const count = value.materials === null ? '未记录' : `${value.materials.length} 项`;
  return <div className="settings-expand">
    <Row readOnly title="回执编号" meta={value.id}/>
    <Row readOnly title="记录状态" meta={recordStates[value.status] || '未记录'}/>
    <Row readOnly title="调用归属" sub={value.origin?.project_id || '项目未记录'} meta={value.origin?.turn_id || value.origin?.record_id}/>
    <Row readOnly title="输入 / 输出" meta={formatReceiptUsage(value.usage)}/>
    <Row readOnly title="花费" meta={formatReceiptCost(value.cost)}/>
    <Row readOnly title={value.materials_basis === 'frozen_turn_inputs' ? '轮次冻结材料' : '轮次材料'} meta={count}/>
    {value.materials?.map((item, index) => <Row readOnly key={`${item.type}:${item.id}:${index}`}
      title={item.availability === 'available' ? item.title || item.id : '不可读取'}
      sub={`${item.type === 'recognition' ? '认识' : item.type === 'document' ? '整理稿' : '原件'} · ${item.project_id || '项目未记录'}`}
      meta={`${item.id || '身份未记录'}${Number.isInteger(item.revision) ? ` · r${item.revision}` : ''}${item.current_private ? ' · 当前私密' : ''}`}/>)}
    <Row readOnly title="私密排除" meta={value.excluded === null ? '未记录' : `${value.excluded.length} 项`}/>
    {value.excluded?.map((item, index) => <Row readOnly key={`${item.type}:${item.id}:${index}`} title="已排除 · 详情不可读取"
      sub={item.project_id || '项目未记录'} meta={item.id || '身份未记录'}/>)}
    {value.calls?.map(call => <div key={`${call.turn_id}:${call.model_request_id}`}><Row readOnly title={call.model || '模型未记录'}
      sub={`${callStates[call.status] || '状态未记录'} · ${formatReceiptUsage(call.usage)} · ${formatReceiptCost(call.cost)}`}
      meta={call.model_request_id}/><Row readOnly title="模型回执" meta={call.receipt_id || '逻辑回执未记录'}/>{call.attempts?.map(attempt => <Row readOnly key={attempt.id}
        title={`尝试 ${attempt.number} · ${callStates[attempt.status] || '状态未记录'}`}
        sub={`${receiptTime(attempt.at, timezone)} · ${formatReceiptUsage(attempt.usage)} · ${formatReceiptCost(attempt.cost)}`}
        meta={attempt.id}/>)}</div>)}
  </div>;
}

export function ReceiptInspection({ api, timezone, month, scopeKey = 'privacy-receipts', scopeMode = 'egress' }) {
  const [period, setPeriod] = useState('month'), [purpose, setPurpose] = useState('');
  const [cursors, setCursors] = useState([null]), [position, setPosition] = useState(0), [epoch, setEpoch] = useState(0);
  const [page, setPage] = useState(null), [error, setError] = useState(false);
  const [options, setOptions] = useState([]);
  const [selected, setSelected] = useState(null), [detail, setDetail] = useState(null), [detailError, setDetailError] = useState(false);
  const selectedRef = useRef(null), detailRequest = useRef(null);
  const cursor = cursors[position] || null;
  const gate = useRequestScope(`${scopeKey}:${timezone}:${month}:${period}:${purpose}:${cursor}:${scopeMode}`), scope = gate.scope;
  useEffect(() => { setCursors([null]); setPosition(0); }, [month, timezone]);
  useEffect(() => {
    const token = gate.issue('page', scope), identity = deviceIdentity(), controller = new AbortController();
    setPage(null); setError(false); selectedRef.current = null; setSelected(null); setDetail(null); detailRequest.current?.abort();
    Promise.resolve().then(() => api.receiptHistory({ month: period === 'month' ? month : undefined, timezone,
      purpose: purpose || undefined, cursor, scope: scopeMode }, controller.signal)).then(value => {
      if (!Array.isArray(value?.rows) || !validCount(value.total) || !Array.isArray(value.purposes)
          || value.scope !== scopeMode || value.rows.some(row => typeof row.id !== 'string')) throw new Error('receipt_page_invalid');
      if (gate.isCurrent(token) && isDeviceIdentity(identity)) {
        setPage(value); setOptions(previous => [...value.purposes,
          ...previous.filter(item => item.id === purpose && !value.purposes.some(option => option.id === item.id))]);
      }
    }).catch(() => { if (gate.isCurrent(token) && isDeviceIdentity(identity)) setError(true); });
    return () => { controller.abort(); detailRequest.current?.abort(); gate.invalidate('page'); gate.invalidate('detail'); };
  }, [api, scope, epoch]);
  function filter(setter, value) { setter(value); setCursors([null]); setPosition(0); }
  async function inspect(identity, retry = false) {
    if (selectedRef.current === identity && !retry) {
      selectedRef.current = null; setSelected(null); setDetail(null); detailRequest.current?.abort(); gate.invalidate('detail'); return;
    }
    detailRequest.current?.abort();
    const token = gate.issue('detail', scope), device = deviceIdentity(), controller = new AbortController();
    detailRequest.current = controller; selectedRef.current = identity;
    setSelected(identity); setDetail(null); setDetailError(false);
    try {
      const value = await api.receiptDetail(identity, controller.signal, { month: period === 'month' ? month : undefined, timezone, scope: scopeMode });
      if (value?.id !== identity || value.scope !== scopeMode || !value.origin || typeof value.origin !== 'object'
          || value.materials !== null && !Array.isArray(value.materials)
          || value.excluded !== null && !Array.isArray(value.excluded)) throw new Error('receipt_detail_invalid');
      if (gate.isCurrent(token) && isDeviceIdentity(device) && selectedRef.current === identity) setDetail(value);
    } catch { if (gate.isCurrent(token) && isDeviceIdentity(device) && selectedRef.current === identity) setDetailError(true); }
  }
  function next() {
    if (!page?.next_cursor) return;
    setCursors(values => [...values.slice(0, position + 1), page.next_cursor]); setPosition(value => value + 1);
  }
  return <section aria-label={scopeMode === 'models' ? '模型记录' : '外发记录'}>
    <h2 className="settings-caption">{scopeMode === 'models' ? '模型记录' : '外发记录'}</h2>
    <div className="settings-fields">
      <label>时间<select value={period} onChange={event => filter(setPeriod, event.target.value)}><option value="month">本月</option><option value="all">全部</option></select></label>
      <label>用途<select value={purpose} onChange={event => filter(setPurpose, event.target.value)}><option value="">全部用途</option>
        {options.map(value => <option key={value.id} value={value.id}>{value.label}</option>)}</select></label>
    </div>
    <p className="settings-support-empty">{period === 'month' ? month : '全部时间'} · {timezone} · {scopeMode === 'models' ? '本机与远程模型' : '外发'} · {page ? `${page.total} 份` : error ? '读取失败' : '读取中'}</p>
    <div className="settings-card">{page?.rows.map(row => {
      const time = receiptTime(row.at, timezone);
      return <div key={row.id}><Row title={row.purpose || '用途未记录'} expanded={selected === row.id}
        sub={`${time} · ${row.model || row.client || '模型未记录'}`} meta={formatReceiptCost(row.cost)} onOpen={() => inspect(row.id)}/>
        {selected === row.id && (detail ? <ReceiptDetail value={detail} timezone={timezone}/> : detailError ? <p role="alert">回执暂不可读 · <button type="button" onClick={() => inspect(row.id, true)}>重试</button></p> : <p role="status">核对材料中</p>)}
      </div>;
    })}{page?.rows.length === 0 && <p className="settings-support-empty">此范围没有已核验回执</p>}</div>
    {error && <p role="alert">外发记录未读取 · <button type="button" onClick={() => setEpoch(value => value + 1)}>重试</button></p>}
    <div className="settings-support-inline"><button type="button" className="settings-support-button" disabled={!page || position === 0}
      onClick={() => setPosition(value => value - 1)}>上一页</button><span>第 {position + 1} 页</span>
      <button type="button" className="settings-support-button" disabled={!page?.next_cursor} onClick={next}>下一页</button>
      <button type="button" className="settings-support-button" onClick={() => setEpoch(value => value + 1)}>刷新</button></div>
  </section>;
}
