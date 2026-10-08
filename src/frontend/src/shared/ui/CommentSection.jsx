import './memory.css';

const identity = value => typeof value === 'string' && value.length > 0 && value === value.trim() && !/[\u0000-\u001f]/.test(value);
const integer = value => Number.isSafeInteger(value) && value >= 0;
const fields = (value, keys) => value !== null && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));

// DTO coordinates address Python codepoints. Markdown AST and ProseMirror use UTF16.
// This is a presentation consistency check; the backend owner qualifies provenance.
export function codepointRange(text, range) {
  if (typeof text !== 'string' || !fields(range, ['start', 'end']) || !integer(range.start) || !integer(range.end) || range.start >= range.end) return null;
  const points = [...text];
  if (range.end > points.length) return null;
  const start = points.slice(0, range.start).join('').length;
  return { start, end: start + points.slice(range.start, range.end).join('').length };
}
export function commentSectionRange({ markdown, documentId, documentRevision, commentSection }) {
  if (!identity(documentId) || !integer(documentRevision) || documentRevision < 1
    || !fields(commentSection, ['document_id', 'document_revision', 'coordinate_space', 'count', 'heading'])
    || commentSection.document_id !== documentId || commentSection.document_revision !== documentRevision
    || commentSection.coordinate_space !== 'document_markdown_v1' || !integer(commentSection.count) || commentSection.count < 1) return null;
  const range = codepointRange(markdown, commentSection.heading);
  return range && markdown.slice(range.start, range.end) === '## 评论区' ? { ...range, count: commentSection.count } : null;
}
export function isCommentSource(value) {
  if (!fields(value, ['type', 'id', 'project_id', 'revision', 'coordinate_space', 'ordinal', 'start', 'end', 'quote'])) return false;
  const coordinates = { original_item: 'workspace_source_text_v1', original_source: 'source_content_v1' };
  return Object.hasOwn(coordinates, value.type) && coordinates[value.type] === value.coordinate_space
    && identity(value.id) && identity(value.project_id) && integer(value.revision) && value.revision > 0
    && integer(value.ordinal) && value.ordinal > 0 && integer(value.start) && integer(value.end) && value.end > value.start
    && typeof value.quote === 'string' && value.quote.trim().length > 0 && [...value.quote].length === value.end - value.start;
}
export function CommentCount({ count }) {
  return <span className="ui-comment-count" contentEditable={false} aria-label={`${count}条评论`}>{count}</span>;
}
export function commentCountElement(count, ownerDocument) {
  const element = ownerDocument.createElement('span');
  element.className = 'ui-comment-count'; element.setAttribute('contenteditable', 'false');
  element.setAttribute('aria-label', `${count}条评论`); element.textContent = String(count);
  return element;
}
