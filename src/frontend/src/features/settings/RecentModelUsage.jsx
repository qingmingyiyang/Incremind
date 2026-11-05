import { useState } from 'react';
import { formatReceiptCost, ReceiptInspection, ReceiptScopeNote, useMonthlyUsage } from './ReceiptInspection';

export function RecentModelUsage({ api, onRecords }) {
  const usage = useMonthlyUsage(api, 'model-usage', 'models'), summary = usage.data;
  const [records, setRecords] = useState(false);
  return <section className="settings-model-usage" aria-label="模型使用">
    <h2 className="settings-caption">本月<span className="settings-caption-action settings-support-inline">
      <button type="button" aria-expanded={records} onClick={() => setRecords(value => !value)}>模型记录</button>
      <button type="button" onClick={onRecords}>外发记录 ›</button></span></h2>
    <div className="settings-card settings-spending"><div className="settings-spending-total">
      <strong>{formatReceiptCost(summary?.cost)}</strong><span>{summary ? `已登记模型调用 ${summary.model_call_count} 次` : usage.error ? '读取失败' : '读取中'}
        <small>{summary ? `本机与远程 · 已登记尝试 ${summary.wire_attempt_count} 次` : ''}</small></span></div>
      <ReceiptScopeNote summary={summary}/>
      {summary && <p className="settings-support-empty">外部记忆交付 {summary.external_delivery_count} 次 · 费用未记录</p>}
      {summary?.call_count_unknown_receipts > 0 && <p className="settings-support-empty">另有 {summary.call_count_unknown_receipts} 份未登记调用数</p>}
      {summary?.purposes.map(group => <div className="settings-spending-row" key={group.id}><span>{group.label}</span>
        <span className="settings-spending-meter" aria-hidden="true" title={`${group.model_call_count} 次模型调用`}><span style={{ width: `${group.model_call_count / Math.max(summary.model_call_count, 1) * 100}%` }}/></span>
        <span className="ui-row-meta">{formatReceiptCost(group.cost)}</span></div>)}
      {usage.error && <p role="alert">使用记录未读取 · <button type="button" onClick={usage.refresh}>重试</button></p>}
      <button type="button" className="settings-support-button" onClick={usage.refresh}>刷新用量</button>
    </div>{records && <ReceiptInspection api={api} timezone={usage.timezone} month={usage.month} scopeMode="models" scopeKey="model-records"/>}</section>;
}
