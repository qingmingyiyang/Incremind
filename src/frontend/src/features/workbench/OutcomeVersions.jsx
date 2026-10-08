import { useEffect, useRef, useState } from 'react';
import { Icon, Row, StatusDot } from '../../shared/ui';
import { MarkdownBody } from '../../shared/ui/MarkdownBody';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { libraryApi } from '../library/libraryApi';
import { OutcomeChanges } from './OutcomeChanges';

export function OutcomeVersions({ projectId, documentId }) {
  const gate = useRequestScope(`${projectId}:${documentId}`), scope = gate.scope;
  const [loaded, setLoaded] = useState(null), [failure, setFailure] = useState(null);
  const [reading, setReading] = useState(null), active = useRef(null);
  useEffect(() => () => active.current?.abort(), [scope]);
  async function reload() {
    const token = gate.issue('versions', scope);
    if (!token) return;
    active.current?.abort();
    const controller = new AbortController(); active.current = controller;
    setLoaded(null); setFailure(null);
    try {
      const result = await libraryApi.outcomeVersions(projectId, documentId, { signal: controller.signal });
      if (!Array.isArray(result?.items)) throw new Error('invalid_versions');
      if (gate.isCurrent(token) && !controller.signal.aborted) setLoaded({ scope, items: result.items });
    } catch {
      if (gate.isCurrent(token) && !controller.signal.aborted) setFailure({ scope });
    }
  }
  useEffect(() => { reload(); }, [scope]);
  async function open(row) {
    const token = gate.issue('version-text', scope);
    if (!token) return;
    setReading({ scope, row, loading: true });
    try {
      const result = await libraryApi.drill(projectId, 'note', row.document_id);
      if (typeof result?.note?.markdown !== 'string' || result.note.document_id !== row.document_id) throw new Error('invalid_version_text');
      if (gate.isCurrent(token)) setReading({ scope, row, markdown: result.note.markdown });
    } catch {
      if (gate.isCurrent(token)) setReading({ scope, row, error: true });
    }
  }
  function close() { gate.invalidate('version-text'); setReading(null); }
  const items = loaded?.scope === scope ? loaded.items : [];
  const current = reading?.scope === scope ? reading : null;
  return <section aria-label="成果版本">
    {failure?.scope === scope ? <button type="button" className="ui-filter" aria-label="重试读取版本" onClick={reload}><Icon name="review-reask"/><span>读取未完成 · 重试</span></button>
      : loaded?.scope !== scope ? <StatusDot state="processing"/> : null}
    {items.map(row => <Row key={row.document_id} title={row.version === 1 ? '首版' : `v${row.version}`}
      meta={row.created_at} selected={current?.row.document_id === row.document_id} onOpen={() => open(row)}/>) }
    {current && <section aria-label="上一版">
      <button type="button" className="ui-filter" aria-label="关闭上一版" onClick={close}><Icon name="close"/></button>
      {current.loading ? <StatusDot state="processing"/> : current.error
        ? <button type="button" className="ui-filter" aria-label="重试读取上一版" onClick={() => open(current.row)}><Icon name="review-reask"/><span>读取未完成 · 重试</span></button>
        : <div style={{ color: 'var(--muted)' }}><OutcomeChanges changes={current.row.changes}/><MarkdownBody>{current.markdown}</MarkdownBody></div>}
    </section>}
  </section>;
}
