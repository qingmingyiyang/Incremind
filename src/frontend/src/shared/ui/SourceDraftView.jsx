import { useEffect, useRef, useState } from "react";
import { DraftConflictView } from "./DraftConflictView";
import { SourceHighlight, selectedSourceEvidence } from "./sourceEvidence";
import "./memory.css";
const textOf = (entry) => typeof entry === "string" ? entry : entry.text || entry.content || entry.claim || entry.title || "";
export function SourceDraftView({ source = "", draft = {}, onAddFact, onOpenSource, conflict, disabled = false, highlightFacts = false, sourceTitle = "原文", sourceControls, editor, renderedEditor }) {
  const [highlight, setHighlight] = useState(null), [error, setError] = useState("");
  const sourceRef = useRef(null), markRef = useRef(null);
  useEffect(() => { setHighlight(null); setError(""); }, [source, draft]);
  useEffect(() => { if (highlight) markRef.current?.scrollIntoView?.({ block: "center", behavior: "smooth" }); }, [highlight]);
  function locate(entry) {
    const evidence = entry.evidence;
    if (highlightFacts && !validRange(source, evidence)) return;
    if (!evidence?.quote || !source.includes(evidence.quote)) { setError("未找到原句 · 打开原件"); return; }
    setError(""); setHighlight(evidence); sourceRef.current?.focus({ preventScroll: true });
  }
  function addFact() {
    const result = selectedSourceEvidence(sourceRef.current, source);
    if (result.error) { setError("选句无效 · 重选"); return; }
    setError(""); onAddFact({ text: result.evidence.quote, evidence: result.evidence });
  }
  return <div className="ui-source-draft"><section aria-label="原文"><div className="ui-source-heading"><h3>{sourceTitle}</h3>{sourceControls}{onAddFact && <button type="button" disabled={disabled} onMouseDown={(event) => event.preventDefault()} onClick={addFact}>加入事实</button>}{onOpenSource && <button type="button" onClick={onOpenSource}>打开原件</button>}</div>{error && <p className="ui-source-error" role="alert">{error}</p>}<div className="ui-source-text" tabIndex={-1} ref={sourceRef}>{highlightFacts ? <FactHighlights source={source} facts={draft.facts || []} current={highlight} markRef={markRef}/> : <SourceHighlight source={source} quote={highlight?.quote} start={highlight?.start} markRef={markRef}/>}</div></section><section aria-label="整理"><h3>整理</h3>{conflict && <DraftConflictView {...conflict} disabled={disabled || conflict.disabled} compact/>}{renderedEditor ? renderedEditor({ locateFact: locate }) : <div className="ui-draft-fields"><div><span className="ui-draft-label">标题</span><h4>{draft.title}</h4></div><div><span className="ui-draft-label">摘要</span><p>{draft.summary}</p></div><div><span className="ui-draft-label">事实</span><ul>{(draft.facts || []).map((entry, index) => <li key={index}>{entry.evidence?.quote ? <button type="button" aria-label={`定位原文：${textOf(entry)}`} onClick={() => locate(entry)}>{textOf(entry)} <span aria-hidden="true">↩</span></button> : textOf(entry)}</li>)}</ul></div><div><span className="ui-draft-label">待办</span><ul>{(draft.todos || []).map((entry, index) => <li key={index}>{textOf(entry)}</li>)}</ul></div></div>}{editor}</section></div>;
}
function validRange(source, evidence) {
  const points = Array.from(source);
  return evidence && Number.isInteger(evidence.start) && Number.isInteger(evidence.end) && evidence.start >= 0 && evidence.end > evidence.start && evidence.end <= points.length && points.slice(evidence.start, evidence.end).join('') === evidence.quote;
}
function FactHighlights({ source, facts, current, markRef }) {
  const ranges = facts.map(item => item.evidence).filter(item => validRange(source, item)).sort((a, b) => a.start - b.start);
  const points = Array.from(source);
  const parts = []; let end = 0;
  for (const range of ranges) {
    if (range.start < end) continue;
    parts.push(points.slice(end, range.start).join(''));
    const selected = current?.start === range.start && current?.end === range.end;
    parts.push(<mark key={`${range.start}:${range.end}`} data-current={selected} ref={selected ? markRef : null} style={selected ? undefined : { outline: 'none' }}>{points.slice(range.start, range.end).join('')}</mark>);
    end = range.end;
  }
  parts.push(points.slice(end).join('')); return parts;
}
export default SourceDraftView;
