import './citedAnswer.css';
import { Icon } from './Icon';
const labels = { insight: '认识', summary: '摘要', note: '整理稿', source: '原件', inspiration: '灵感' };
const markers = () => /\[(\d+)\]|【(\d+)】/g;
const unverifiedReasons = new Set(['invalid_first_fact_requires_correction', 'first_fact_adoption_quote_missing',
  'grounded_statement_duplicate', 'grounded_statement_without_support', 'grounded_statement_uncertain',
  'grounded_date_note_kind_invalid', 'grounded_calendar_provenance_invalid']);

function unverifiedMarks(answer, spans) {
  if (!Array.isArray(spans)) return [];
  // 服务端坐标是Unicode代码点；DOM字符串和Range使用UTF-16位置。
  const offsets = [0];
  for (const point of answer) offsets.push(offsets.at(-1) + point.length);
  const marks = [];
  let end = 0;
  for (const span of spans) {
    if (!span || !Number.isInteger(span.start) || !Number.isInteger(span.end)
      || !Number.isInteger(span.claim) || span.claim < 1 || span.start < end || span.end <= span.start
      || span.end >= offsets.length || !Array.isArray(span.reason_codes) || !span.reason_codes.length
      || span.reason_codes.some(code => !unverifiedReasons.has(code))) return [];
    const left = offsets[span.start], right = offsets[span.end];
    if (answer.slice(left, right) !== '【未核实】') return [];
    marks.push({ start: left, end: right, claim: span.claim, codes: span.reason_codes });
    end = span.end;
  }
  return marks;
}
export function externalCitationUrl(citation) {
  const value = citation?.url;
  if (typeof value !== 'string' || !/^https?:\/\//i.test(value) || /\s/.test(value)) return null;
  try {
    const url = new URL(value);
    return url.hostname && !url.username && !url.password ? value : null;
  } catch { return null; }
}
export function answerWithoutCitationMarks(answer = '', citations = []) {
  const known = new Set(citations.map(citation => citation.n));
  return answer.replace(markers(), (mark, bracket, square) => known.has(Number(bracket || square)) ? '' : mark);
}
// Presentation only. A caller supplies real layer navigation when available.
export function CitedAnswer({ answer = '', citations = [], unverifiedSpans, onOpenCitation, citationHref, onCopyAnswer }) {
  const byNumber = new Map(citations.map(citation => [citation.n, citation]));
  const used = new Set(), parts = [];
  function reference(citation, key) {
    const historical = citation.historical === true;
    const stale = citation.stale === true;
    const label = `引用 ${citation.n} · ${labels[citation.layer]} · ${citation.title}${historical ? ' · 当时' : ''}${stale ? ' · 过时' : ''}`;
    const mark = <>{citation.bookshelf && <Icon name="book" size={10}/>}{citation.n}{historical && <span className="ui-citation-temporal">当时</span>}{stale && <span className="ui-citation-temporal">过时</span>}</>;
    const external = externalCitationUrl(citation);
    if (external) return <sup key={key} className="ui-citation-external"><a href={external} target="_blank" rel="noopener noreferrer" aria-label={label} title={`${external}\n${citation.quote}`}>{mark} <span className="ui-citation-domain">{new URL(external).hostname}</span></a></sup>;
    if (citationHref && onOpenCitation) return <sup key={key}><a href={citationHref} aria-label={label} title={citation.quote} onClick={event => { event.preventDefault(); onOpenCitation(citation); }}>{mark}</a></sup>;
    return onOpenCitation
      ? <sup key={key}><button type="button" aria-label={label} title={citation.quote} onClick={() => onOpenCitation(citation)}>{mark}</button></sup>
      : <sup key={key} aria-label={label} title={citation.quote}>{mark}</sup>;
  }
  const pattern = markers(), tokens = [];
  let cursor = 0, match;
  while ((match = pattern.exec(answer))) {
    const citation = byNumber.get(Number(match[1] || match[2]));
    if (!citation) continue;
    tokens.push({ start: match.index, end: match.index + match[0].length,
      node: reference(citation, `inline-${match.index}`) });
    used.add(citation.n);
  }
  for (const mark of unverifiedMarks(answer, unverifiedSpans)) tokens.push({ ...mark,
    node: <span key={`unverified-${mark.start}`} className="ui-unverified-claim" role="note"
      aria-label={`未核实；原因码：${mark.codes.join(', ')}`} title={mark.codes.join(', ')}
      data-unverified-claim={mark.claim}>【未核实】</span> });
  for (const token of tokens.sort((left, right) => left.start - right.start)) {
    if (token.start < cursor) continue;
    parts.push(answer.slice(cursor, token.start), token.node);
    cursor = token.end;
  }
  parts.push(answer.slice(cursor));
  for (const citation of citations) if (!used.has(citation.n)) parts.push(' ', reference(citation, `tail-${citation.n}`));
  function copySelection(event) {
    if (!onCopyAnswer || !event.clipboardData) return;
    const selection = globalThis.getSelection?.();
    if (!selection || selection.isCollapsed || !selection.rangeCount) return;
    const fragments = [];
    for (let index = 0; index < selection.rangeCount; index += 1) {
      const range = selection.getRangeAt(index);
      if (!event.currentTarget.contains(range.startContainer) || !event.currentTarget.contains(range.endContainer)) return;
      const fragment = range.cloneContents();
      fragment.querySelectorAll('sup').forEach(node => node.remove());
      fragments.push(fragment.textContent);
    }
    const text = fragments.join('');
    if (!text) return;
    try { event.clipboardData.setData('text/plain', text); } catch { return; }
    event.preventDefault(); onCopyAnswer();
  }
  return <div className="ui-cited-answer" onCopy={copySelection}>{parts}</div>;
}
export default CitedAnswer;
