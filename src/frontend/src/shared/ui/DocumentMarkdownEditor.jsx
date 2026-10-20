import { useEffect, useRef, useState } from 'react';
import { recognitionApi } from '../api/recognitionApi';
import { userStorageKey } from '../api/deviceTransport';
import { readStoredJson, removeStoredItem, writeStoredJson } from '../lib/browserStorage';
import { useRequestScope } from '../lib/useRequestScope';
import { DraftConflictView } from './DraftConflictView';
import { RichMarkdownEditor } from './RichMarkdownEditor';
import { DocumentDeliveryAction } from './DocumentDeliveryAction';

const validDocument = value => typeof value?.markdown === 'string' && Number.isInteger(value.revision) && value.revision > 0;
const sameDocument = (value, expected, id) => validDocument(value) && (value.document_id || value.id) === id
  && value.revision === expected.revision && value.markdown === expected.markdown;
const withoutComments = value => { const { comment_section: _old, ...next } = value; return next; };
const sameDraft = (value, expected) => validDocument(value?.base) && validDocument(expected?.base)
  && value.markdown === expected.markdown && value.base.revision === expected.base.revision && value.base.markdown === expected.base.markdown;
const draftRecord = (base, markdown) => ({ markdown, base: { revision: base.revision, markdown: base.markdown } });
// The consumer supplies the opened document and owns the surrounding read model.
export function DocumentMarkdownEditor(props) {
  const storageKey = userStorageKey(`chriptmas-v2-document-draft:${encodeURIComponent(props.projectId)}:${encodeURIComponent(props.documentId)}`);
  return <DocumentEditorSession key={storageKey} {...props} storageKey={storageKey}/>;
}
function DocumentEditorSession({ projectId, documentId, document, disabled = false, label, evidence, onEvidence, onSaved, readDocument, renderHeading, storageKey }) {
  const gate = useRequestScope(storageKey), scope = gate.scope;
  const reader = useRef(readDocument); reader.current = readDocument;
  const [initial] = useState(() => {
    const value = readStoredJson(storageKey).value;
    const stored = validDocument(value?.base) && typeof value.markdown === 'string' && value.markdown !== value.base.markdown ? value : null;
    const recovered = stored && validDocument(document) && stored.markdown !== document.markdown;
    return { stored, markdown: recovered ? stored.markdown : document.markdown,
      base: recovered ? withoutComments({ ...document, revision: stored.base.revision, markdown: stored.base.markdown }) : document };
  });
  const [base, setBase] = useState(initial.base), [markdown, setMarkdown] = useState(initial.markdown), [conflict, setConflict] = useState(null);
  const stored = useRef(initial.stored), current = useRef(null);
  const [saving, setSaving] = useState(false), [error, setError] = useState('');
  current.current = { base, markdown, saving, conflict, disabled };
  function clearDraft(expected) {
    // 只清理本次拥有的版本，迟到保存不能删除另一处新编辑。
    if (sameDraft(readStoredJson(storageKey).value, expected)) removeStoredItem(storageKey);
    if (sameDraft(stored.current, expected)) stored.current = null;
  }
  function storeDraft(nextBase, nextMarkdown) {
    if (!validDocument(nextBase)) return;
    if (nextMarkdown === nextBase.markdown) { clearDraft(stored.current); return; }
    const value = draftRecord(nextBase, nextMarkdown);
    if (writeStoredJson(storageKey, value)) stored.current = value;
  }
  function changeMarkdown(value) {
    current.current = { ...current.current, markdown: value };
    storeDraft(current.current.base, value); setMarkdown(value);
  }
  useEffect(() => {
    if (validDocument(initial.base) && initial.markdown === initial.base.markdown) clearDraft(initial.stored);
  }, []);
  function refreshComments(expected, onMatch) {
    const read = reader.current; if (!read) return;
    const request = gate.issue('comment-section', scope);
    Promise.resolve().then(read).then(value => {
      if (!gate.isCurrent(request)) return;
      const latest = current.current;
      if (!onMatch && validDocument(value) && (value.document_id || value.id) === documentId
        && value.revision > latest.base.revision && value.markdown === latest.markdown
        && latest.markdown !== latest.base.markdown && sameDraft(stored.current, draftRecord(latest.base, latest.markdown))) {
        // 正文已被权威读取确认时采用新基线，评论仍经下面的完整身份校验。
        const next = withoutComments(value); expected = next;
        current.current = { ...latest, base: next }; setBase(next); clearDraft(stored.current);
      }
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
    const submitted = draftRecord(base, local);
    const request = gate.issue('save', scope); current.current.saving = true; setSaving(true); setError('');
    try {
      const result = await recognitionApi.saveDocument({ projectId, documentId, expectedRevision: base.revision, markdown: local });
      if (!gate.isCurrent(request)) return;
      if (!validDocument({ ...result, markdown: local })) { setError('读取未完成'); return; }
      const next = withoutComments({ ...base, ...result, markdown: local }), latest = current.current;
      current.current = { ...latest, base: next }; setBase(next);
      if (latest.markdown === local) clearDraft(submitted);
      else if (sameDraft(readStoredJson(storageKey).value, stored.current)) storeDraft(next, latest.markdown);
      onSaved?.(next);
      refreshComments(next);
    } catch (failure) {
      if (!gate.isCurrent(request)) return;
      if (failure.status !== 409) { setError('保存未完成'); return; }
      try {
        const value = await recognitionApi.loadDocument({ projectId, documentId });
        if (gate.isCurrent(request)) {
          if (validDocument(value)) {
            const server = withoutComments(value); setConflict({ base, local: current.current.markdown, server });
            refreshComments(server, commentSection => setConflict(current => current?.server === server
              ? { ...current, server: { ...server, comment_section: commentSection } } : current));
          } else setError('读取未完成');
        }
      } catch { if (gate.isCurrent(request)) setError('读取未完成'); }
    } finally { if (gate.isCurrent(request)) setSaving(false); }
  }
  function choose(server) {
    const nextMarkdown = server ? conflict.server.markdown : conflict.local, nextBase = conflict.server;
    if (server) clearDraft(stored.current); else storeDraft(nextBase, nextMarkdown);
    current.current = { ...current.current, base: nextBase, markdown: nextMarkdown, conflict: null };
    setMarkdown(nextMarkdown); setBase(nextBase); setConflict(null);
  }
  return <>{error && <p role="alert">{error}</p>}{conflict && <DraftConflictView compact baseDraft={conflict.base} editor={{ markdown: conflict.local }} server={conflict.server} fields={[["markdown", "整理稿"]]} onChoose={choose} disabled={saving || disabled}/>}
    <RichMarkdownEditor value={markdown} onChange={changeMarkdown} onSave={save} disabled={disabled || saving || Boolean(conflict) || !validDocument(base)} label={label} evidence={evidence} onEvidence={onEvidence} renderHeading={renderHeading}
      projectId={projectId} documentId={documentId} documentRevision={base.revision} documentMarkdown={base.markdown} commentSection={base.comment_section}
      toolbarActions={<DocumentDeliveryAction projectId={projectId} documentId={documentId} revision={base.revision}
        needsSave={markdown !== base.markdown || Boolean(conflict)} disabled={disabled || saving || !validDocument(base)}
        isSavedRevision={revision => { const value = current.current; return !value.disabled && !value.saving && !value.conflict
          && validDocument(value.base) && value.base.revision === revision && value.markdown === value.base.markdown; }}/>}/>
  </>;
}
