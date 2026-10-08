import './citedAnswer.css';
import { Icon } from './Icon';
const labels = { insight: '认识', summary: '摘要', note: '整理稿', source: '原件', inspiration: '灵感' };
const markers = () => /\[(\d+)\]|【(\d+)】/g;
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
export function CitedAnswer({ answer = '', citations = [], onOpenCitation, citationHref, onCopyAnswer }) {
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
  const pattern = markers();
  let cursor = 0, match;
  while ((match = pattern.exec(answer))) {
    const citation = byNumber.get(Number(match[1] || match[2]));
    if (!citation) continue;
    parts.push(answer.slice(cursor, match.index), reference(citation, `inline-${match.index}`));
    used.add(citation.n); cursor = match.index + match[0].length;
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
