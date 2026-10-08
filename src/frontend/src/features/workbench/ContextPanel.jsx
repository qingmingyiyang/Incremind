import { FocusPanel, LadderTrace, Row } from '../../shared/ui';
import { useEffect, useRef, useState } from 'react';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { contextFeedbackApi } from './contextFeedbackApi';
import { formatModelCost } from '../../shared/lib/modelCost';
import { externalCitationUrl } from '../../shared/ui/CitedAnswer';
import './ContextPanel.css';

const labels = { insight: '认识', persona: '我', inspiration: '灵感', summary: '摘要', note: '整理稿', source: '原件', instruction: '指令', question: '问题', history: '对话', expert_brief: '专家结论', style: '写法', previous: '上一版' };
const colors = { insight: '--ink', persona: '--ink2', inspiration: '--ink2', summary: '--muted', note: '--line2', source: '--red', instruction: '--sunk', question: '--line', history: '--muted', expert_brief: '--ink2', style: '--ink2', previous: '--line2' };
const number = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
const shown = value => number(value) ? String(value) : '—';
const percent = (value, window) => number(value) && number(window) && window > 0 ? `${Math.round(value / window * 1000) / 10}%` : '—';

export function ContextPanel({ kind = 'ask', receipt = {}, onClose, onOpenCitation }) {
  const context = receipt.context ?? {};
  const parts = Array.isArray(context.parts) ? context.parts : [];
  const used = parts.length && parts.every(part => number(part.tokens)) ? parts.reduce((sum, part) => sum + part.tokens, 0) : undefined;
  const window = context.window, reserve = context.reserve;
  const free = number(window) && number(used) && number(reserve) ? Math.max(0, window - used - reserve) : undefined;
  const citations = Array.isArray(receipt.citations) ? receipt.citations : [];
  const entries = Array.isArray(context.entries) ? context.entries : citations;
  const binding = context.feedback;
  const key = `${binding?.project_id ?? ''}:${binding?.turn_id ?? ''}`;
  const requests = useRequestScope(key), scope = requests.scope;
  const [feedback, setFeedback] = useState({ key, ids: new Set() });
  const pending = useRef(new Set());
  const struck = feedback.key === key ? feedback.ids : new Set();
  useEffect(() => {
    if (!binding?.turn_id || !binding?.project_id) return;
    const token = requests.issue('feedback', scope);
    contextFeedbackApi.list(binding).then(value => {
      if (requests.isCurrent(token)) setFeedback(current => ({ key, ids: new Set([
        ...(current.key === key ? current.ids : []), ...(value.items ?? []).map(item => item.object_id)]) }));
    }).catch(() => {});
  }, [key]);
  async function strike(entry) {
    const slot = `${key}:${entry.id}`;
    if (!requests.isScopeCurrent(scope) || pending.current.has(slot) || struck.has(entry.id)) return;
    pending.current.add(slot);
    const token = requests.issue(`strike:${entry.id}`, scope);
    try {
      await contextFeedbackApi.strike(binding, entry);
      if (requests.isCurrent(token)) setFeedback(current => ({ key,
        ids: new Set([...(current.key === key ? current.ids : []), entry.id]) }));
    } catch { /* Keep the action available for an explicit retry. */ }
    finally { pending.current.delete(slot); }
  }
  const egress = context.egress ?? {};
  const usage = receipt.model_usage ?? {};
  const input = usage.input_tokens ?? usage.prompt_tokens, output = usage.output_tokens ?? usage.completion_tokens;
  // excluded_sources includes budget and duplicate-evidence exclusions, not a privacy ledger.
  const excluded = egress.excluded_private;
  const authorization = (egress.consent_scope ?? receipt.consent_basis?.scope) === 'global_setting' ? '全局' : '—';
  const visibleParts = parts.filter(part => !(['persona', 'style'].includes(part.key) && part.count === 0 && part.tokens === 0));
  const rows = kind === 'do' ? visibleParts : [...visibleParts, { key: 'reserve', tokens: reserve }, { key: 'free', tokens: free }];
  return <FocusPanel title="上下文" onClose={onClose} className="context-panel">
    <div className="context-total" aria-label="总量"><strong>{shown(used)}</strong>{' '}<span>/ {shown(window)}</span>{' '}<span>{percent(used, window)}</span></div>
    {number(window) && window > 0 && number(used) && <div className="context-segments" role="progressbar" aria-label="上下文用量" aria-valuemin={0} aria-valuemax={window} aria-valuenow={used}>
      {rows.filter(part => number(part.tokens) && part.tokens > 0 && part.key !== 'free').map((part, index) => <span key={`${part.key}:${index}`} title={`${labels[part.key] ?? '预留回答'} ${shown(part.tokens)}`} className={part.key === 'reserve' ? 'context-reserve' : ''} style={{ width: `${Math.min(100, part.tokens / window * 100)}%`, backgroundColor: `var(${colors[part.key] ?? '--line2'})` }}/>)}</div>}
    <section className="context-classification" aria-label="分类">{rows.map((part, index) => <div className="context-part" key={`${part.key}:${index}`}><span className={`context-swatch ${part.key === 'reserve' ? 'context-reserve' : ''}`} style={{ backgroundColor: `var(${colors[part.key] ?? '--line2'})` }}/><span>{labels[part.key] ?? ({ reserve: '预留回答', free: '空余' }[part.key] ?? '—')}</span><span>{shown(part.count)}</span><span>{shown(part.tokens)}</span><span>{percent(part.tokens, window)}</span></div>)}</section>
    {kind === 'ask' && <details className="context-section"><summary>阶梯</summary><LadderTrace trace={receipt.trace ?? []} layers={receipt.layers} citations={citations} onOpenCitation={onOpenCitation}/></details>}
    {(kind === 'ask' || entries.some(entry => entry.supplemented)) && <details className="context-section" aria-label="条目"><summary>条目</summary>{entries.map((entry, index) => {
      const url = externalCitationUrl(entry);
      return <Row key={`${entry.id}:${entry.n}:${index}`} title={entry.title || '—'} className={struck.has(entry.id) ? 'context-item-struck' : ''} dot={<span className="context-item-layer">{entry.supplemented ? '补' : entry.persona ? '我' : labels[entry.layer] ?? '—'}</span>} meta={number(entry.tokens) ? String(entry.tokens) : number(entry.n) ? String(entry.n) : '—'} trailing={<>{url && <a className="ui-row-meta" style={{color:'var(--red)'}} href={url} target="_blank" rel="noopener noreferrer">{url}</a>}{entry.supplemented && binding && <button type="button" className="context-strike" aria-label={`划掉 ${entry.title || '认识'}`} disabled={struck.has(entry.id)} onClick={() => strike(entry)}>×</button>}</>} readOnly={!onOpenCitation || Boolean(url)} onOpen={() => onOpenCitation?.(entry)}/>;
    })}</details>}
    {kind === 'ask' && number(context.bookshelf?.hits) && context.bookshelf.hits > 0 && <details className="context-section" aria-label="书架"><summary>书架 {context.bookshelf.hits}</summary>{(context.bookshelf.spines ?? []).map((spine, index) => <Row key={`${spine.kind}:${spine.id}:${index}`} title={spine.title || '—'} meta={[labels[spine.layer], spine.scene, typeof spine.date === 'string' ? spine.date.slice(0, 10) : null].filter(Boolean).join(' · ')} readOnly={!onOpenCitation} onOpen={() => onOpenCitation?.(spine)}/>)}</details>}
    <section className="context-egress" aria-label="外发"><Row title="模型" meta={typeof egress.model === 'string' ? egress.model : typeof receipt.model === 'string' ? receipt.model : '—'} readOnly/><Row title="授权" meta={authorization} readOnly/><Row title="私密排除" meta={shown(excluded)} readOnly/><Row title="实际用量" meta={`入 ${shown(input)} · 出 ${shown(output)}`} readOnly/><Row title="花费" meta={formatModelCost(receipt.model_cost)} readOnly/></section>
  </FocusPanel>;
}
export default ContextPanel;
