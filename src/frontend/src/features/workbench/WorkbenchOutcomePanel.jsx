import { useRef, useState } from 'react';
import { FocusPanel, StatusDot, Icon, MarkdownBody } from '../../shared/ui';
import { DocumentMarkdownEditor } from '../../shared/ui/DocumentMarkdownEditor';
import { richHeadingSections } from '../../shared/ui/RichMarkdownEditor';
import { splitMarkdown } from '../../shared/ui/richMarkdown';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { libraryApi } from '../library/libraryApi';
import { OutcomeChanges } from './OutcomeChanges';
import { OutcomeVersions } from './OutcomeVersions';

export function WorkbenchOutcomePanel({ projectId, documentId, document, loading, error, onClose, task = {}, qualified = false, previous, onRetryPrevious, onContinue }) {
  const gate = useRequestScope(`${projectId}:${documentId}`), scope = gate.scope;
  const [menu, setMenu] = useState(false), [versions, setVersions] = useState(false), [comparison, setComparison] = useState(null);
  const headings = useRef(new Map());
  async function compare(path, retry = false) {
    const key = JSON.stringify(path);
    if (!retry && comparison?.scope === scope && comparison.key === key) { gate.invalidate('previous'); setComparison(null); return; }
    const token = gate.issue('previous', scope);
    if (!token || !task.continues?.document_id) return;
    setComparison({ scope, key, path, loading: true });
    try {
      const baseline = retry ? await onRetryPrevious?.() : previous;
      if ((!retry && !qualified) || baseline?.document_id !== task.continues.document_id || !Number.isSafeInteger(baseline.revision)
        || baseline.revision <= 0 || typeof baseline.markdown !== 'string') throw new Error('invalid_previous');
      const section = richHeadingSections(splitMarkdown(baseline.markdown)).find(row => JSON.stringify(row.path) === key);
      const added = (task.changes ?? []).some(row => row.kind === 'added' && JSON.stringify(row.path) === key);
      // 新增的小节没有旧正文；重复路径或更新小节缺失仍然是读取错误。
      if (section ? !section.unique : !added) throw new Error('previous_section_missing');
      if (gate.isCurrent(token)) setComparison({ scope, key, path, body: section?.body ?? '' });
    } catch { if (gate.isCurrent(token)) setComparison({ scope, key, path, error: true }); }
  }
  const renderHeading = ({ path }) => {
    const key = JSON.stringify(path), change = (task.changes ?? []).find(row => JSON.stringify(row.path) === key);
    if (!change) return null;
    const current = comparison?.scope === scope && comparison.key === key ? comparison : null;
    return { before: <button ref={node => { if (node) headings.current.set(key, node); else headings.current.delete(key); }} type="button" className="ui-filter" aria-label={`对照上一版 ${path.join(' / ')}`} onClick={() => compare(path)}>
      {change.kind === 'added' ? <Icon name="plus" size={11} style={{ color: 'var(--red)' }}/> : <span className="ui-status-dot" style={{ width: 6, height: 6, background: 'var(--red)' }}/>}</button>,
      after: current && <section aria-label={`上一版 ${path.join(' / ')}`} style={{ color: 'var(--muted)' }}>{current.loading ? <StatusDot state="processing"/> : current.error
        ? <button type="button" onClick={() => compare(path, true)}>读取未完成 · 重试</button> : <MarkdownBody>{current.body}</MarkdownBody>}</section> };
  };
  const actions = !loading && !error && document && <><button type="button" className="ui-filter" aria-label="成果更多" onClick={() => setMenu(value => !value)}><Icon name="more"/></button>{menu && <div>
    {qualified && <button type="button" className="ui-filter" onClick={() => { setMenu(false); onContinue?.(document.title || task.title); }}>接着写</button>}
    <button type="button" className="ui-filter" onClick={() => { setMenu(false); setVersions(value => !value); }}>版本列表</button>
  </div>}</>;
  return <FocusPanel title="成果" onClose={onClose} actions={actions}>{loading ? <StatusDot state="processing"/> : error ? <p role="alert">成果未打开</p> : document && <>
    <OutcomeChanges changes={task.changes} onSelect={path => headings.current.get(JSON.stringify(path))?.scrollIntoView?.({ block: 'center' })}/>
    <DocumentMarkdownEditor key={`${projectId}:${documentId}`} projectId={projectId} documentId={documentId} document={document} label="成果正文" readDocument={async () => (await libraryApi.drill(projectId, 'note', documentId)).note} renderHeading={renderHeading}/>
    {versions && <OutcomeVersions projectId={projectId} documentId={documentId}/>}</>}</FocusPanel>;
}
