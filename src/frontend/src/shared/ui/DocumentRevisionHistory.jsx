import { useEffect, useState } from 'react';
import { loadEditableDocumentRevisions } from '../../features/rebuild/libraryOverviewApi';
import { productFetch, userStorageKey } from '../api/deviceTransport';
import { useRequestScope } from '../lib/useRequestScope';
import { Icon } from './Icon';
import { Row } from './Row';
import { StatusDot } from './StatusDot';

const operations = { create: '创建', user_edit: '修改', ai_patch: '自动修订', archive: '归档', restore: '恢复' };
const authors = { system: '系统', user: '用户', admin: '管理员' };
const spaceKey = () => userStorageKey('document-history');

export function DocumentRevisionHistory({ projectId, documentId, revision, onClose }) {
  const [userSpace, setUserSpace] = useState(spaceKey);
  const gate = useRequestScope(`${userSpace}:${projectId}:${documentId}`), scope = gate.scope;
  const [loaded, setLoaded] = useState(null), [error, setError] = useState(false), [refresh, setRefresh] = useState(0);
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
  const value = loaded?.scope === scope ? loaded.value : null, loading = !value && !error;
  return <section aria-label="整理稿历史" aria-busy={loading}>
    <div className="ui-rich-actions"><strong>历史</strong>{loading && <StatusDot state="processing"/>}
      <button type="button" aria-label="刷新历史" disabled={loading} onClick={() => setRefresh(value => value + 1)}><Icon name="review-reask" size={16}/></button>
      <button type="button" aria-label="关闭历史" autoFocus onClick={onClose}><Icon name="close" size={16}/></button>
    </div>
    {error && <p role="alert">读取未完成 <button type="button" onClick={() => setRefresh(value => value + 1)}>重试</button></p>}
    {value && [...value.revisions].sort((left, right) => right.revision - left.revision).map(entry => <Row key={entry.revision} readOnly
      title={<>v{entry.revision} · {operations[entry.operation] || entry.operation}{entry.conflict?.status === 'detected' && <> · 冲突 {entry.conflict.conflict_blocks.length}</>}</>}
      sub={authors[entry.author] || entry.author}
      meta={entry.created_at && <time dateTime={entry.created_at} title={entry.created_at}>{new Date(entry.created_at).toLocaleString('zh-CN', { hour12: false })}</time>}/>)}
  </section>;
}
