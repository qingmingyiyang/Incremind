import { useEffect, useRef, useState } from 'react';
import { loadEditableDocumentRevisions } from '../../features/rebuild/libraryOverviewApi';
import { libraryApi } from '../../features/library/libraryApi';
import { productFetch, userStorageKey } from '../api/deviceTransport';
import { useRequestScope } from '../lib/useRequestScope';
import { Icon } from './Icon';
import { Row } from './Row';
import { StatusDot } from './StatusDot';
import { MarkdownBody } from './MarkdownBody';

const operations = { create: '创建', user_edit: '修改', ai_patch: '自动修订', archive: '归档', restore: '恢复' };
const authors = { system: '系统', user: '用户', admin: '管理员' };
const spaceKey = () => userStorageKey('document-history');

export function DocumentRevisionHistory({ projectId, documentId, revision, onClose, onRestore, onRefreshDocument, canRestore = false }) {
  const [userSpace, setUserSpace] = useState(spaceKey);
  const gate = useRequestScope(`${userSpace}:${projectId}:${documentId}`), scope = gate.scope;
  const [loaded, setLoaded] = useState(null), [error, setError] = useState(false), [refresh, setRefresh] = useState(0);
  const [reading, setReading] = useState(null), [actionError, setActionError] = useState(null), [busy, setBusy] = useState(false);
  const active = useRef(null), pending = useRef(null);
  useEffect(() => {
    pending.current = null; setBusy(false); setReading(null); setActionError(null);
    return () => { active.current?.abort(); gate.invalidate('revision-text'); gate.invalidate('restore'); };
  }, [scope]);
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
    const token = gate.issue('history', scope), controller = new AbortController();
    const current = () => spaceKey() === userSpace && gate.isCurrent(token);
    setLoaded(null); setError(false);
    loadEditableDocumentRevisions({ documentId, projectId,
      fetchImpl: (url, options) => productFetch(url, { ...options, signal: controller.signal }),
    }).then(value => {
      if (!current()) return;
      if (value.document_id !== documentId || !Array.isArray(value.revisions)) throw new Error('document_history_invalid');
      setLoaded({ scope, value });
    }).catch(() => { if (current()) setError(true); });
    return () => { controller.abort(); gate.invalidate('history'); };
  }, [scope, revision, refresh]);
  async function open(entry) {
    if (pending.current) return;
    const token = gate.issue('revision-text', scope);
    if (!token) return;
    active.current?.abort();
    const controller = new AbortController(); active.current = controller;
    setReading({ scope, revision: entry.revision, loading: true }); setActionError(null);
    try {
      const value = await libraryApi.documentRevision(projectId, documentId, entry.revision, { signal: controller.signal });
      if (spaceKey() !== userSpace || !gate.isCurrent(token)) return;
      if (value.document_id !== documentId || value.revision !== entry.revision || typeof value.markdown !== 'string') throw new Error('document_revision_invalid');
      setReading({ scope, revision: entry.revision, markdown: value.markdown });
    } catch {
      if (spaceKey() === userSpace && gate.isCurrent(token) && !controller.signal.aborted) setReading({ scope, revision: entry.revision, error: true });
    }
  }
  async function restore() {
    if (pending.current || !canRestore || !onRestore || reading?.scope !== scope || typeof reading.markdown !== 'string') return;
    const token = gate.issue('restore', scope);
    if (!token) return;
    pending.current = token; setBusy(true); setActionError(null);
    try {
      await onRestore(reading.revision);
      if (spaceKey() !== userSpace || !gate.isCurrent(token)) return;
      setReading(null); setRefresh(value => value + 1);
    } catch (failure) {
      if (spaceKey() === userSpace && gate.isCurrent(token)) setActionError({ scope, conflict: failure.status === 409,
        message: failure.status === 409 ? '内容已变化 · 刷新' : '恢复未完成 · 重试' });
    } finally {
      if (pending.current === token) pending.current = null;
      if (gate.isCurrent(token)) setBusy(false);
    }
  }
  async function reload() {
    if (pending.current) return;
    const token = gate.issue('restore', scope);
    if (!token) return;
    pending.current = token; setBusy(true); setActionError(null);
    try {
      if (onRefreshDocument) await onRefreshDocument();
      if (spaceKey() !== userSpace || !gate.isCurrent(token)) return;
      gate.invalidate('revision-text'); active.current?.abort(); setReading(null); setRefresh(value => value + 1);
    } catch {
      if (spaceKey() === userSpace && gate.isCurrent(token)) setActionError({ scope, message: '读取未完成 · 重试', conflict: true });
    } finally {
      if (pending.current === token) pending.current = null;
      if (gate.isCurrent(token)) setBusy(false);
    }
  }
  const value = loaded?.scope === scope ? loaded.value : null, loading = !value && !error;
  const current = reading?.scope === scope ? reading : null, failure = actionError?.scope === scope ? actionError : null;
  return <section aria-label="整理稿历史" aria-busy={loading}>
    <div className="ui-rich-actions"><strong>历史</strong>{loading && <StatusDot state="processing"/>}
      <button type="button" aria-label="刷新历史" disabled={loading || busy || !canRestore && Boolean(onRefreshDocument)} onClick={reload}><Icon name="review-reask" size={16}/></button>
      <button type="button" aria-label="关闭历史" autoFocus disabled={busy} onClick={onClose}><Icon name="close" size={16}/></button>
    </div>
    {error && <p role="alert">读取未完成 <button type="button" onClick={() => setRefresh(value => value + 1)}>重试</button></p>}
    {failure && <p role="alert">{failure.message}{failure.conflict && <button type="button" disabled={busy || !canRestore} onClick={reload}>刷新</button>}</p>}
    {value && [...value.revisions].sort((left, right) => right.revision - left.revision).map(entry => <Row key={entry.revision} readOnly={busy}
      selected={current?.revision === entry.revision} onOpen={() => open(entry)}
      title={<>v{entry.revision} · {operations[entry.operation] || entry.operation}{entry.conflict?.status === 'detected' && <> · 冲突 {entry.conflict.conflict_blocks.length}</>}</>}
      sub={authors[entry.author] || entry.author}
      meta={entry.created_at && <time dateTime={entry.created_at} title={entry.created_at}>{new Date(entry.created_at).toLocaleString('zh-CN', { hour12: false })}</time>}/>)}
    {current && <section aria-label={`修订 ${current.revision} 正文`}>
      <div className="ui-rich-actions"><strong>v{current.revision}</strong>{current.loading && <StatusDot state="processing"/>}
        {typeof current.markdown === 'string' && onRestore && <button type="button" disabled={busy || !canRestore}
          title={canRestore ? undefined : '先保存当前修改'} onClick={restore}>{busy ? '恢复中' : '恢复为此版'}</button>}
      </div>
      {current.error ? <button type="button" disabled={busy} onClick={() => open(current)}>读取未完成 · 重试</button>
        : typeof current.markdown === 'string' && <MarkdownBody>{current.markdown}</MarkdownBody>}
    </section>}
  </section>;
}
