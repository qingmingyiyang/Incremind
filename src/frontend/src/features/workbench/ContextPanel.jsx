import { FocusPanel, LadderTrace, Row } from '../../shared/ui';
import { Fragment, useEffect, useRef, useState } from 'react';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { deviceIdentity, isDeviceIdentity, userStorageKey } from '../../shared/api/deviceTransport';
import { contextFeedbackApi } from './contextFeedbackApi';
import { formatModelCost } from '../../shared/lib/modelCost';
import { externalCitationUrl } from '../../shared/ui/CitedAnswer';
import './ContextPanel.css';
import { ExternalUsage } from './ExternalTaskProgress';

const labels = { insight: '认识', persona: '我', inspiration: '灵感', summary: '摘要', note: '整理稿', source: '原件', instruction: '指令', question: '问题', history: '对话', expert_brief: '专家结论', style: '写法', previous: '上一版' };
const colors = { insight: '--ink', persona: '--ink2', inspiration: '--ink2', summary: '--muted', note: '--line2', source: '--red', instruction: '--sunk', question: '--line', history: '--muted', expert_brief: '--ink2', style: '--ink2', previous: '--line2' };
const number = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
const shown = value => number(value) ? String(value) : '—';
const percent = (value, window) => number(value) && number(window) && window > 0 ? `${Math.round(value / window * 1000) / 10}%` : '—';

export function ContextPanel({ kind = 'ask', receipt = {}, onClose, onOpenCitation, onSettings, projectName, navigation }) {
  const context = receipt.context ?? {};
  const parts = Array.isArray(context.parts) ? context.parts : [];
  const used = parts.length && parts.every(part => number(part.tokens)) ? parts.reduce((sum, part) => sum + part.tokens, 0) : undefined;
  const window = context.window, reserve = context.reserve;
  const free = number(window) && number(used) && number(reserve) ? Math.max(0, window - used - reserve) : undefined;
  const citations = Array.isArray(receipt.citations) ? receipt.citations : [];
  const entries = Array.isArray(context.entries) ? context.entries : citations;
  const binding = context.feedback;
  const [identityEpoch, setIdentityEpoch] = useState(0);
  const actor = deviceIdentity(), userSpace = userStorageKey(binding?.project_id ?? '');
  const key = `${userSpace}:${binding?.turn_id ?? ''}:${identityEpoch}`;
  const requests = useRequestScope(key), scope = requests.scope;
  const [feedback, setFeedback] = useState({ key, ids: new Set() });
  const [reading, setReading] = useState({ key, busy: false, error: '' });
  const [writing, setWriting] = useState({ key, ids: new Set() });
  const [failures, setFailures] = useState({ key, items: new Map() });
  const pending = useRef(new Set());
  const currentEntries = useRef(entries);
  currentEntries.current = entries;
  const struck = feedback.key === key ? feedback.ids : new Set();
  const failed = failures.key === key ? failures.items : new Map();
  const loading = reading.key === key && reading.busy;
  const current = token => requests.isCurrent(token) && isDeviceIdentity(actor)
    && userStorageKey(binding?.project_id ?? '') === userSpace;
  const entryKey = entry => `${entry.id}:${entry.object_revision}`;
  function markWriting(entry, active) {
    setWriting(previous => {
      const ids = new Set(previous.key === key ? previous.ids : []);
      if (active) ids.add(entryKey(entry)); else ids.delete(entryKey(entry));
      return { key, ids };
    });
  }
  useEffect(() => {
    const changed = () => { requests.invalidate('feedback'); setIdentityEpoch(value => value + 1); };
    globalThis.window.addEventListener('chriptmas-user-space', changed);
    globalThis.window.addEventListener('chriptmas-device-required', changed);
    return () => {
      globalThis.window.removeEventListener('chriptmas-user-space', changed);
      globalThis.window.removeEventListener('chriptmas-device-required', changed);
    };
  }, [requests]);
  useEffect(() => {
    pending.current.clear();
    setFeedback({ key, ids: new Set() }); setFailures({ key, items: new Map() });
    setWriting({ key, ids: new Set() }); setReading({ key, busy: false, error: '' });
    loadFeedback();
    return () => requests.invalidate('feedback');
  }, [key]);
  async function loadFeedback() {
    if (!binding?.turn_id || !binding?.project_id) return null;
    const token = requests.issue('feedback', scope);
    if (!token) return null;
    setReading({ key, busy: true, error: '' });
    try {
      const value = await contextFeedbackApi.list(binding);
      if (!current(token)) return null;
      const ids = new Set((value.items ?? []).map(item => `${item.object_id}:${item.object_revision}`));
      setFeedback(previous => ({ key, ids: new Set([...(previous.key === key ? previous.ids : []), ...ids]) }));
      setFailures(previous => ({ key, items: new Map([...(previous.key === key ? previous.items : [])]
        .filter(([, failure]) => !ids.has(entryKey(failure.entry)))) }));
      return ids;
    } catch {
      if (current(token)) setReading({ key, busy: false, error: '操作记录读取未完成' });
      return null;
    } finally {
      if (current(token)) setReading(previous => ({ ...previous, busy: false }));
    }
  }
  async function strike(entry) {
    const slot = `${key}:${entryKey(entry)}`;
    if (!requests.isScopeCurrent(scope) || !isDeviceIdentity(actor)
        || pending.current.has(slot) || struck.has(entryKey(entry))) return;
    pending.current.add(slot);
    const token = requests.issue(`strike:${entry.id}`, scope);
    markWriting(entry, true);
    setFailures(previous => {
      const items = new Map(previous.key === key ? previous.items : []);
      items.delete(entryKey(entry)); return { key, items };
    });
    try {
      await contextFeedbackApi.strike(binding, entry);
      if (current(token) && currentEntries.current.some(row => row.id === entry.id && row.object_revision === entry.object_revision && row.supplemented))
        setFeedback(previous => ({ key, ids: new Set([...(previous.key === key ? previous.ids : []), entryKey(entry)]) }));
    } catch (reason) {
      if (current(token)) setFailures(previous => {
        const items = new Map(previous.key === key ? previous.items : []);
        items.set(entryKey(entry), { entry, changed: ['context_entry_changed', 'context_feedback_conflict', 'workbench_not_found'].includes(reason.message) });
        return { key, items };
      });
    } finally {
      pending.current.delete(slot);
      if (current(token)) markWriting(entry, false);
    }
  }
  async function retryStrike(entry) {
    const ids = await loadFeedback();
    if (ids === null || ids.has(entryKey(entry)) || !isDeviceIdentity(actor)
        || !currentEntries.current.some(row => row.id === entry.id && row.object_revision === entry.object_revision && row.supplemented)) return;
    await strike(entry);
  }
  const egress = context.egress ?? {};
  const usage = receipt.model_usage ?? {};
  const input = usage.input_tokens ?? usage.prompt_tokens, output = usage.output_tokens ?? usage.completion_tokens;
  // excluded_sources includes budget and duplicate-evidence exclusions, not a privacy ledger.
  const excluded = egress.excluded_private;
  const authorization = (egress.consent_scope ?? receipt.consent_basis?.scope) === 'global_setting' ? '全局' : '—';
  const visibleParts = parts.filter(part => !(['persona', 'style'].includes(part.key) && part.count === 0 && part.tokens === 0));
  const rows = kind === 'do' ? visibleParts : [...visibleParts, { key: 'reserve', tokens: reserve }, { key: 'free', tokens: free }];
  return <FocusPanel title="本次用了什么" onClose={onClose} className="context-panel">
    {navigation}
    {reading.key === key && reading.error && <p role="alert">{reading.error} · <button type="button" disabled={loading} onClick={loadFeedback}>重试</button></p>}
    <div className="context-total" aria-label="总量"><strong>{shown(used)}</strong>{' '}<span>/ {shown(window)}</span>{' '}<span>{percent(used, window)}</span></div>
    {number(window) && window > 0 && number(used) && <div className="context-segments" role="progressbar" aria-label="上下文用量" aria-valuemin={0} aria-valuemax={window} aria-valuenow={used}>
      {rows.filter(part => number(part.tokens) && part.tokens > 0 && part.key !== 'free').map((part, index) => <span key={`${part.key}:${index}`} title={`${labels[part.key] ?? '预留回答'} ${shown(part.tokens)}`} className={part.key === 'reserve' ? 'context-reserve' : ''} style={{ width: `${Math.min(100, part.tokens / window * 100)}%`, backgroundColor: `var(${colors[part.key] ?? '--line2'})` }}/>)}</div>}
    <section className="context-classification" aria-label="分类">{rows.map((part, index) => <div className="context-part" key={`${part.key}:${index}`}><span className={`context-swatch ${part.key === 'reserve' ? 'context-reserve' : ''}`} style={{ backgroundColor: `var(${colors[part.key] ?? '--line2'})` }}/><span>{labels[part.key] ?? ({ reserve: '预留回答', free: '空余' }[part.key] ?? '—')}</span><span>{shown(part.count)}</span><span>{shown(part.tokens)}</span><span>{percent(part.tokens, window)}</span></div>)}</section>
    {kind === 'ask' && <details className="context-section" open><summary>阶梯</summary><LadderTrace trace={receipt.trace ?? []} layers={receipt.layers} citations={citations} onOpenCitation={onOpenCitation}/></details>}
    {(kind === 'ask' || entries.some(entry => entry.supplemented)) && <details className="context-section" aria-label="条目"><summary>条目</summary>{entries.map((entry, index) => {
      const url = externalCitationUrl(entry);
      const failure = failed.get(entryKey(entry));
      const sending = writing.key === key && writing.ids.has(entryKey(entry));
      return <Fragment key={`${entry.id}:${entry.n}:${index}`}><Row title={entry.title || '—'} className={struck.has(entryKey(entry)) ? 'context-item-struck' : ''} dot={<span className="context-item-layer">{entry.supplemented ? '补' : entry.persona ? '我' : labels[entry.layer] ?? '—'}</span>} meta={number(entry.tokens) ? String(entry.tokens) : number(entry.n) ? String(entry.n) : '—'} trailing={<>{url && <a className="ui-row-meta" style={{color:'var(--red)'}} href={url} target="_blank" rel="noopener noreferrer">{url}</a>}{entry.supplemented && binding && <button type="button" className="context-strike" aria-label={`划掉 ${entry.title || '认识'}`} aria-busy={sending} disabled={struck.has(entryKey(entry)) || sending || loading} onClick={() => strike(entry)}>×</button>}</>} readOnly={!onOpenCitation || Boolean(url)} onOpen={() => onOpenCitation?.(entry)}/>
        {failure && <p role="alert">{failure.changed ? '条目已变化' : '划掉未完成'} · <button type="button" disabled={sending || loading}
          aria-label={`${failure.changed ? '关闭面板' : '重试划掉'} ${entry.title || '认识'}`}
          onClick={failure.changed ? onClose : () => retryStrike(entry)}>{failure.changed ? '关闭' : '重试'}</button></p>}
      </Fragment>;
    })}</details>}
    {kind === 'ask' && number(context.bookshelf?.hits) && context.bookshelf.hits > 0 && <details className="context-section" aria-label="书架"><summary>书架 {context.bookshelf.hits}</summary>{(context.bookshelf.spines ?? []).map((spine, index) => <Row key={`${spine.kind}:${spine.id}:${index}`} title={spine.title || '—'} meta={[labels[spine.layer], spine.scene, typeof spine.date === 'string' ? spine.date.slice(0, 10) : null].filter(Boolean).join(' · ')} readOnly={!onOpenCitation} onOpen={() => onOpenCitation?.(spine)}/>)}</details>}
    <section className="context-egress" aria-label="这次的设置"><Row title="模型" meta={typeof egress.model === 'string' ? egress.model : typeof receipt.model === 'string' ? receipt.model : '—'} readOnly={!onSettings} onOpen={() => onSettings?.('model')}/><Row title="外发" meta={authorization} readOnly={!onSettings} onOpen={() => onSettings?.('privacy')}/>{onSettings && <Row title="范围" meta={projectName} onOpen={() => onSettings('project')}/>}<Row title="私密排除" meta={shown(excluded)} readOnly/>{receipt.executor && receipt.executor !== 'builtin' ? <ExternalUsage receipt={receipt}/> : <><Row title="实际用量" meta={`入 ${shown(input)} · 出 ${shown(output)}`} readOnly/><Row title="花费" meta={formatModelCost(receipt.model_cost)} readOnly/></>}</section>
  </FocusPanel>;
}
export default ContextPanel;
