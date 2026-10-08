import { useEffect, useRef, useState } from 'react';
import { FocusPanel, Icon, Row } from '../../shared/ui';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { libraryApi } from './libraryApi';

const empty = [];
function gapItems(value) {
  if (!Array.isArray(value?.items)) throw new Error('invalid_gaps');
  const ids = new Set();
  for (const row of value.items) {
    if (typeof row?.id !== 'string' || !row.id || ids.has(row.id)
      || typeof row.text !== 'string' || !row.text
      || !(row.scene === null || typeof row.scene === 'string')
      || !Number.isSafeInteger(row.count) || row.count < 1
      || typeof row.last_at !== 'string' || !Number.isFinite(Date.parse(row.last_at))) throw new Error('invalid_gaps');
    ids.add(row.id);
  }
  return value.items;
}

export function useGaps(projectId, refresh) {
  const gate = useRequestScope(projectId), scope = gate.scope;
  const [loaded, setLoaded] = useState(null), [failure, setFailure] = useState(null);
  const active = useRef(null);
  useEffect(() => () => active.current?.abort(), [scope]);
  function invalidate() { gate.invalidate('gaps'); active.current?.abort(); }
  async function reload() {
    const request = gate.issue('gaps', scope);
    if (!request) return;
    active.current?.abort();
    const controller = new AbortController(); active.current = controller;
    try {
      const items = gapItems(await libraryApi.gaps(projectId, { signal: controller.signal }));
      if (!controller.signal.aborted && gate.isCurrent(request)) { setLoaded({ scope, items }); setFailure(null); }
    } catch {
      if (!controller.signal.aborted && gate.isCurrent(request)) setFailure({ scope, message: '读取未完成 · 重试' });
    }
  }
  useEffect(() => { reload(); }, [scope, refresh]);
  function remove(id) {
    if (!gate.isScopeCurrent(scope)) return;
    invalidate();
    setLoaded(value => value?.scope === scope ? { ...value, items: value.items.filter(row => row.id !== id) } : value);
  }
  return { items: loaded?.scope === scope ? loaded.items : empty,
    error: failure?.scope === scope ? failure.message : '', reload, remove, invalidate };
}

export function Gaps({ projectId, gaps, onClose, onOpen }) {
  const gate = useRequestScope(projectId), scope = gate.scope;
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const active = useRef(null), locked = useRef(false), opening = useRef(false);
  useEffect(() => () => active.current?.abort(), []);
  async function dismiss(id) {
    if (locked.current) return;
    const request = gate.issue('dismiss', scope);
    if (!request) return;
    locked.current = true; setBusy(true); setError(''); gaps.invalidate();
    const controller = new AbortController(); active.current = controller;
    try {
      await libraryApi.dismissGap(projectId, id, { signal: controller.signal });
      if (!controller.signal.aborted && gate.isCurrent(request)) gaps.remove(id);
    } catch (failure) {
      if (!controller.signal.aborted && gate.isCurrent(request)) {
        setError(failure.status === 409 ? '内容已变化 · 重新读取' : '操作未完成 · 重试');
        if (failure.status === 409) await gaps.reload();
      }
    } finally {
      if (gate.isCurrent(request)) { locked.current = false; setBusy(false); }
    }
  }
  return <FocusPanel title="待补" className="library-focus library-gaps" onClose={onClose}>
    {gaps.items.map(row => <Row key={row.id} title={row.text}
      sub={`${row.count} 次 · ${row.last_at.slice(5, 10).replace('-', '·')}`} readOnly={busy}
      onOpen={() => { if (!locked.current && !opening.current) { opening.current = true; onOpen(row); } }}
      trailing={<button type="button" className="library-gap-dismiss" aria-label={`丢弃 ${row.text}`} disabled={busy} onClick={() => dismiss(row.id)}><Icon name="close" size={14}/></button>}/>)}
    {(error || gaps.error) && <div className="library-error" role="alert"><span>{error || gaps.error}</span><button type="button" disabled={busy} onClick={() => { setError(''); gaps.reload(); }}>重试</button></div>}
  </FocusPanel>;
}
