import { useEffect, useRef, useState } from 'react';
import { Icon, Row, StatusDot } from '../../shared/ui';
import { MarkdownBody } from '../../shared/ui/MarkdownBody';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { userStorageKey } from '../../shared/api/deviceTransport';
import { libraryApi } from '../library/libraryApi';
import { OutcomeChanges } from './OutcomeChanges';

const spaceKey = () => userStorageKey('outcome-versions');
const validRevision = value => Number.isSafeInteger(value) && value > 0;

export function OutcomeVersions({ projectId, documentId, canRestore = false, onRestored, onRestoringChange }) {
  const [userSpace, setUserSpace] = useState(spaceKey);
  const gate = useRequestScope(`${userSpace}:${projectId}:${documentId}`), scope = gate.scope;
  const [loaded, setLoaded] = useState(null), [failure, setFailure] = useState(null);
  const [reading, setReading] = useState(null), [actionError, setActionError] = useState(null), [busy, setBusy] = useState(false);
  const active = useRef(null), textRequest = useRef(null), pending = useRef(null);
  useEffect(() => {
    const changed = () => setUserSpace(spaceKey());
    window.addEventListener('chriptmas-user-space', changed);
    window.addEventListener('chriptmas-device-required', changed);
    return () => {
      window.removeEventListener('chriptmas-user-space', changed);
      window.removeEventListener('chriptmas-device-required', changed);
    };
  }, []);
  useEffect(() => {
    pending.current = null; setBusy(false); setReading(null); setActionError(null);
    return () => { active.current?.abort(); textRequest.current?.abort(); gate.invalidate('versions'); gate.invalidate('version-text'); gate.invalidate('restore-version'); };
  }, [scope]);
  const currentRequest = token => spaceKey() === userSpace && gate.isCurrent(token);
  async function reload() {
    if (pending.current) return;
    const token = gate.issue('versions', scope);
    if (!token) return;
    active.current?.abort();
    const controller = new AbortController(); active.current = controller;
    textRequest.current?.abort(); gate.invalidate('version-text'); setReading(null); setLoaded(null); setFailure(null); setActionError(null);
    try {
      const result = await libraryApi.outcomeVersions(projectId, documentId, { signal: controller.signal });
      if (!Array.isArray(result?.items) || result.items.some(row => typeof row.document_id !== 'string'
        || !validRevision(row.revision) || !validRevision(row.version))) throw new Error('invalid_versions');
      const latest = result.latest;
      if (result.items.length && (!latest || !validRevision(latest.revision)
        || !result.items.some(row => row.document_id === latest.document_id && row.revision === latest.revision))) throw new Error('invalid_latest_version');
      if (currentRequest(token) && !controller.signal.aborted) setLoaded({ scope, items: result.items, latest });
    } catch {
      if (currentRequest(token) && !controller.signal.aborted) setFailure({ scope });
    }
  }
  useEffect(() => { reload(); }, [scope]);
  async function open(row) {
    if (pending.current) return;
    const token = gate.issue('version-text', scope);
    if (!token) return;
    textRequest.current?.abort();
    const controller = new AbortController(); textRequest.current = controller;
    setReading({ scope, row, loading: true }); setActionError(null);
    try {
      const result = await libraryApi.documentRevision(projectId, row.document_id, row.revision, { signal: controller.signal });
      if (typeof result?.markdown !== 'string' || result.document_id !== row.document_id || result.revision !== row.revision) throw new Error('invalid_version_text');
      if (currentRequest(token)) setReading({ scope, row, markdown: result.markdown });
    } catch {
      if (currentRequest(token) && !controller.signal.aborted) setReading({ scope, row, error: true });
    }
  }
  async function restore() {
    const selected = reading?.scope === scope ? reading : null;
    const latest = loaded?.scope === scope ? loaded.latest : null;
    if (pending.current || !canRestore || !onRestored || !latest || typeof selected?.markdown !== 'string') return;
    const token = gate.issue('restore-version', scope);
    if (!token) return;
    pending.current = token; setBusy(true); setActionError(null); onRestoringChange?.(true);
    try {
      const result = await libraryApi.restoreOutcome(projectId, selected.row.document_id, selected.row.revision, latest);
      if (!currentRequest(token)) return;
      if (typeof result?.document_id !== 'string' || !result.document_id || typeof result.title !== 'string'
        || !validRevision(result.revision) || !validRevision(result.version) || !Array.isArray(result.changes)) throw new Error('invalid_restored_outcome');
      await onRestored({ ...result, continues: { document_id: latest.document_id, version: result.version } });
    } catch (error) {
      if (currentRequest(token)) setActionError({ scope, conflict: error.status === 409,
        message: error.status === 409 ? '内容已变化 · 刷新' : '恢复未完成 · 重试' });
    } finally {
      if (pending.current === token) pending.current = null;
      if (currentRequest(token)) { setBusy(false); onRestoringChange?.(false); }
    }
  }
  function close() { if (pending.current) return; textRequest.current?.abort(); gate.invalidate('version-text'); setReading(null); }
  const items = loaded?.scope === scope ? loaded.items : [];
  const current = reading?.scope === scope ? reading : null, error = actionError?.scope === scope ? actionError : null;
  return <section aria-label="成果版本">
    {failure?.scope === scope ? <button type="button" className="ui-filter" aria-label="重试读取版本" onClick={reload}><Icon name="review-reask"/><span>读取未完成 · 重试</span></button>
      : loaded?.scope !== scope ? <StatusDot state="processing"/> : null}
    {error && <p role="alert">{error.message}{error.conflict && <button type="button" disabled={busy} onClick={reload}>刷新</button>}</p>}
    {items.map(row => <Row key={row.document_id} title={row.version === 1 ? '首版' : `v${row.version}`} readOnly={busy}
      meta={row.created_at} selected={current?.row.document_id === row.document_id} onOpen={() => open(row)}/>) }
    {current && <section aria-label="上一版">
      <div className="ui-rich-actions"><button type="button" className="ui-filter" aria-label="关闭上一版" disabled={busy} onClick={close}><Icon name="close"/></button>
        {typeof current.markdown === 'string' && onRestored && <button type="button" disabled={busy || !canRestore || !loaded?.latest}
          title={canRestore ? undefined : '先保存当前修改'} onClick={restore}>{busy ? '恢复中' : '以此版为准'}</button>}</div>
      {current.loading ? <StatusDot state="processing"/> : current.error
        ? <button type="button" className="ui-filter" aria-label="重试读取上一版" onClick={() => open(current.row)}><Icon name="review-reask"/><span>读取未完成 · 重试</span></button>
        : <div style={{ color: 'var(--muted)' }}><OutcomeChanges changes={current.row.changes}/><MarkdownBody>{current.markdown}</MarkdownBody></div>}
    </section>}
  </section>;
}
