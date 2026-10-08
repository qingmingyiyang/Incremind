import { useEffect, useRef, useState } from 'react';
import { FocusPanel, Icon, Row } from '../../shared/ui';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { libraryApi } from './libraryApi';

const empty = [];
const integer = value => Number.isSafeInteger(value) && value >= 0;
const text = value => typeof value === 'string';
function reviewItems(value) {
  if (!Array.isArray(value?.items)) throw new Error('invalid_reviews');
  const ids = new Set();
  for (const row of value.items) {
    const evidence = row?.evidence;
    if (!text(row?.id) || !row.id || ids.has(row.id) || !text(row.title)
      || !integer(row.revision) || row.revision === 0 || !['reask', 'unused', 'stop'].includes(row.kind)
      || row.effect !== (row.kind === 'unused' ? 'cool' : 'correction')
      || !Array.isArray(evidence?.questions) || !evidence.questions.every(text)) throw new Error('invalid_reviews');
    if (row.kind === 'unused') {
      if (!integer(evidence.sent) || !integer(evidence.used) || !['insight', 'document'].includes(evidence.object?.kind)
        || !text(evidence.object.id) || !evidence.object.id || !integer(evidence.object.revision)
        || evidence.object.revision === 0) throw new Error('invalid_reviews');
    } else if (!integer(evidence.count) || !text(evidence.answer)) throw new Error('invalid_reviews');
    ids.add(row.id);
  }
  return value.items;
}

export function useSignalReviews(projectId, refresh) {
  const gate = useRequestScope(projectId), scope = gate.scope;
  const [loaded, setLoaded] = useState(null), [failure, setFailure] = useState(null);
  const active = useRef(null);
  useEffect(() => () => active.current?.abort(), [scope]);
  async function reload() {
    const request = gate.issue('reviews', scope);
    if (!request) return;
    active.current?.abort();
    const controller = new AbortController(); active.current = controller;
    try {
      const value = reviewItems(await libraryApi.signalReviews(projectId, { signal: controller.signal }));
      if (!controller.signal.aborted && gate.isCurrent(request)) { setLoaded({ scope, items: value }); setFailure(null); }
    } catch {
      if (!controller.signal.aborted && gate.isCurrent(request)) { setLoaded(null); setFailure({ scope, message: '读取未完成 · 重试' }); }
    }
  }
  useEffect(() => { reload(); }, [scope, refresh]);
  function remove(rows) {
    if (!gate.isScopeCurrent(scope)) return;
    setLoaded(value => value?.scope === scope ? { ...value,
      items: value.items.filter(row => !rows.some(done => done.id === row.id && done.revision === row.revision)),
    } : value);
  }
  return { items: loaded?.scope === scope ? loaded.items : empty, error: failure?.scope === scope ? failure.message : '', reload, remove };
}

const evidenceLabel = row => row.kind === 'unused' ? `送 ${row.evidence.sent} · 用 ${row.evidence.used}`
  : `${row.kind === 'reask' ? '重问' : '停下'} ${row.evidence.count}`;

export function SignalReviews({ projectId, reviews, onBack, onOpen, onSelect, drillError, hideEvidence, onChanged }) {
  const { items } = reviews;
  const gate = useRequestScope(projectId), scope = gate.scope;
  const [selected, setSelected] = useState([]), [focused, setFocused] = useState(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [leaving, setLeaving] = useState([]);
  const active = useRef(null), locked = useRef(false), timer = useRef(null);
  useEffect(() => () => { active.current?.abort(); clearTimeout(timer.current); }, []);
  useEffect(() => { setSelected([]); setFocused(null); }, [scope]);
  useEffect(() => {
    setSelected(value => value.filter(chosen => items.some(row => row.id === chosen.id && row.revision === chosen.revision)));
    setFocused(value => items.find(row => row.id === value?.id) || null);
  }, [items]);
  const isSelected = row => selected.some(chosen => chosen.id === row.id && chosen.revision === row.revision);
  const selectedRows = items.filter(row => isSelected(row) && !leaving.includes(row.id));
  async function decide(action) {
    if (locked.current || !selectedRows.length) return;
    const request = gate.issue('decide', scope);
    if (!request) return;
    locked.current = true; setBusy(true); setError('');
    const rows = selectedRows, controller = new AbortController(); active.current = controller;
    let processed = false;
    try {
      const result = await libraryApi.decideSignalReviews(projectId, rows.map(row => ({ id: row.id, action, expected_revision: row.revision })), { signal: controller.signal });
      if (controller.signal.aborted || !gate.isCurrent(request)) return;
      const state = action === 'confirm' ? 'confirmed' : 'dismissed';
      if (!Array.isArray(result?.items) || result.items.length !== rows.length
        || new Set(result.items.map(row => row.id)).size !== rows.length
        || !result.items.every(row => rows.some(item => item.id === row.id) && row.state === state)) throw new Error('invalid_decision');
      processed = true; setLeaving(rows.map(row => row.id)); setSelected([]);
      timer.current = setTimeout(() => {
        if (!gate.isCurrent(request)) return;
        reviews.remove(rows); setLeaving([]); locked.current = false; setBusy(false); onChanged?.();
      }, 200);
    } catch (failure) {
      if (!controller.signal.aborted && gate.isCurrent(request)) {
        setError(failure.status === 409 ? '内容已变化 · 重新读取' : '操作未完成 · 重试');
        if (failure.status === 409) { setSelected([]); await reviews.reload(); }
      }
    } finally {
      if (!processed && gate.isCurrent(request)) { locked.current = false; setBusy(false); }
    }
  }
  return <>
    <main className="library-main library-review-main">
      <header className="library-review-heading"><button type="button" aria-label="返回资料库" onClick={onBack}>‹</button><h2>纠偏 <span>{items.length}</span></h2></header>
      {(reviews.error || error) && <div className="library-error" role="alert"><span>{reviews.error || error}</span>
        <button type="button" disabled={busy} onClick={() => { setError(''); reviews.reload(); }}>重试</button></div>}
      <div className="library-rows library-review-rows" aria-label="纠偏列表">
        {items.map(row => <Row key={row.id} className={`library-review-row ${leaving.includes(row.id) ? 'is-leaving' : ''}`}
          dot={<><label className="library-review-checkbox"><input type="checkbox" aria-label={`选择 ${row.title}`} checked={isSelected(row)} disabled={busy || leaving.includes(row.id)}
            onChange={() => setSelected(value => value.some(chosen => chosen.id === row.id && chosen.revision === row.revision)
              ? value.filter(chosen => chosen.id !== row.id) : [...value.filter(chosen => chosen.id !== row.id), { id: row.id, revision: row.revision }])}/><span><Icon name="check" size={12}/></span></label><Icon name={`review-${row.kind}`} size={14}/></>}
          title={row.title} selected={focused?.id === row.id} onOpen={() => { setFocused(row); onSelect?.(); }}
          trailing={<><span className="library-review-evidence">{evidenceLabel(row)}</span><span className="library-review-effect">{row.effect === 'correction' ? '+1' : '降权'}</span></>}/>) }
      </div>
      <footer className="library-review-footer"><span>已选 {selectedRows.length}</span><div className="library-actions">
        <button type="button" className="library-primary" disabled={busy || !selectedRows.length} onClick={() => decide('confirm')}>确认 {selectedRows.length}</button>
        <button type="button" disabled={busy || !selectedRows.length} onClick={() => decide('dismiss')}>丢弃 {selectedRows.length}</button>
      </div></footer>
    </main>
    {focused && !hideEvidence && !leaving.includes(focused.id) && <FocusPanel className="library-focus library-review-focus" title="纠偏" onClose={() => { setFocused(null); onSelect?.(); }}>
      {drillError && focused.kind === 'unused' && <div className="library-error" role="alert"><span>读取未完成 · 重试</span><button type="button" onClick={() => onOpen(focused.evidence.object)}>重试</button></div>}
      {focused.kind === 'unused' && <><button type="button" className="library-review-open" aria-label={`打开 ${focused.title}`} onClick={() => onOpen(focused.evidence.object)}>{focused.title}<Icon name="open" size={14}/></button><p className="library-review-evidence">{evidenceLabel(focused)}</p></>}
      {focused.evidence.questions.map((question, index) => <p className="library-review-question" key={index}>{question}</p>)}
      {focused.kind !== 'unused' && <p className="library-review-answer">{focused.evidence.answer}</p>}
    </FocusPanel>}
  </>;
}
