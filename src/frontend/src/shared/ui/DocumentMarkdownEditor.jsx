import { useEffect, useRef, useState } from 'react';
import { recognitionApi } from '../api/recognitionApi';
import { useRequestScope } from '../lib/useRequestScope';
import { DraftConflictView } from './DraftConflictView';
import { RichMarkdownEditor } from './RichMarkdownEditor';

const validDocument = value => typeof value?.markdown === 'string' && Number.isInteger(value.revision) && value.revision > 0;
const sameDocument = (value, expected, id) => validDocument(value) && (value.document_id || value.id) === id
  && value.revision === expected.revision && value.markdown === expected.markdown;
const withoutComments = value => { const { comment_section: _old, ...next } = value; return next; };
// The consumer supplies the opened document and owns the surrounding read model.
export function DocumentMarkdownEditor({ projectId, documentId, document, disabled = false, label, evidence, onEvidence, onSaved, readDocument, renderHeading }) {
  const gate = useRequestScope(`${projectId}:${documentId}`), scope = gate.scope;
  const reader = useRef(readDocument); reader.current = readDocument;
  const [base, setBase] = useState(document), [markdown, setMarkdown] = useState(document.markdown), [conflict, setConflict] = useState(null);
  const [saving, setSaving] = useState(false), [error, setError] = useState('');
  function refreshComments(expected, onMatch) {
    const read = reader.current; if (!read) return;
    const request = gate.issue('comment-section', scope);
    Promise.resolve().then(read).then(value => {
      if (!gate.isCurrent(request)) return;
      if (!sameDocument(value, expected, documentId)) {
        if (!onMatch) setBase(current => sameDocument(current, expected, documentId) ? withoutComments(current) : current);
        return;
      }
      if (onMatch) onMatch(value.comment_section);
      else setBase(current => sameDocument(current, expected, documentId)
        ? { ...withoutComments(current), comment_section: value.comment_section } : current);
    }).catch(() => { /* Optional projection never controls save or conflict availability. */ });
  }
  useEffect(() => {
    refreshComments(document);
    return () => gate.invalidate('comment-section');
  }, [projectId, documentId]);
  async function save(local) {
    if (saving || disabled || conflict || !validDocument(base)) return;
    const request = gate.issue('save', scope); setSaving(true); setError('');
    try {
      const result = await recognitionApi.saveDocument({ projectId, documentId, expectedRevision: base.revision, markdown: local });
      if (!gate.isCurrent(request)) return;
      if (!validDocument({ ...result, markdown: local })) { setError('读取未完成'); return; }
      const next = withoutComments({ ...base, ...result, markdown: local }); setBase(next); onSaved?.(next);
      refreshComments(next);
    } catch (failure) {
      if (!gate.isCurrent(request)) return;
      if (failure.status !== 409) { setError('保存未完成'); return; }
      try {
        const value = await recognitionApi.loadDocument({ projectId, documentId });
        if (gate.isCurrent(request)) {
          if (validDocument(value)) {
            const server = withoutComments(value); setConflict({ base, local, server });
            refreshComments(server, commentSection => setConflict(current => current?.server === server
              ? { ...current, server: { ...server, comment_section: commentSection } } : current));
          } else setError('读取未完成');
        }
      } catch { if (gate.isCurrent(request)) setError('读取未完成'); }
    } finally { if (gate.isCurrent(request)) setSaving(false); }
  }
  function choose(server) { setMarkdown(server ? conflict.server.markdown : conflict.local); setBase(conflict.server); setConflict(null); }
  return <>{error && <p role="alert">{error}</p>}{conflict && <DraftConflictView compact baseDraft={conflict.base} editor={{ markdown: conflict.local }} server={conflict.server} fields={[["markdown", "整理稿"]]} onChoose={choose} disabled={saving || disabled}/>}
    <RichMarkdownEditor value={markdown} onChange={setMarkdown} onSave={save} disabled={disabled || saving || Boolean(conflict) || !validDocument(base)} label={label} evidence={evidence} onEvidence={onEvidence} renderHeading={renderHeading}
      documentId={documentId} documentRevision={base.revision} documentMarkdown={base.markdown} commentSection={base.comment_section}/>
  </>;
}
