// Shared presentation/DOM evidence primitives. Callers own draft state and persistence.
export function selectedSourceEvidence(node, source, selection = globalThis.getSelection?.()) {
  const range = selection?.rangeCount ? selection.getRangeAt(0) : null;
  if (!range || !node?.contains(range.startContainer) || !node.contains(range.endContainer)) return { error: "outside" };
  const raw = selection.toString(), quote = raw.trim();
  const before = range.cloneRange();
  before.selectNodeContents(node); before.setEnd(range.startContainer, range.startOffset);
  const start = before.toString().length + raw.indexOf(quote);
  if (!quote || quote.includes("\n") || source.slice(start, start + quote.length) !== quote) return { error: "invalid" };
  return { evidence: { start, end: start + quote.length, quote } };
}
export function SourceHighlight({ source, quote, start, markRef, className }) {
  if (!quote || !source.includes(quote)) return source;
  const position = Number.isSafeInteger(start) && start >= 0 && source.slice(start, start + quote.length) === quote ? start : source.indexOf(quote);
  return <>{source.slice(0, position)}<mark ref={markRef} className={className}>{quote}</mark>{source.slice(position + quote.length)}</>;
}
