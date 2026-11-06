import { StatusDot } from "./StatusDot";
import { externalCitationUrl } from './CitedAnswer';
import "./memory.css";
const levels = [["insight", "认识"], ["summary", "摘要"], ["note", "整理稿"], ["source", "原件"]];
function Citations({ entries, onOpenCitation }) {
  return entries.length > 0 && <ul className="ui-trace-citations">{entries.map(entry => {
    const description = entry.quote || '未直接引用';
    const text = <><span>{entry.title}</span><span>{description}</span></>;
    const url = onOpenCitation && externalCitationUrl(entry);
    return <li key={`${entry.n}:${entry.id}`}>{url ? <a className="ui-trace-citation" href={url} target="_blank" rel="noopener noreferrer">{text}</a>
      : onOpenCitation ? <button className="ui-trace-citation" type="button"
      onClick={() => onOpenCitation(entry)} aria-label={`${entry.title} · ${description}`}>{text}</button>
      : <div className="ui-trace-citation">{text}</div>}</li>;
  })}</ul>;
}
export function LadderTrace({ trace = [], citations = [], layers, onOpenCitation }) {
  const persona = citations.filter((entry) => entry.persona);
  const personaCount = layers?.persona ?? persona.length;
  const inspirations = citations.filter((entry) => entry.layer === 'inspiration');
  const inspirationCount = layers?.inspiration ?? inspirations.length;
  const situations = citations.filter((entry) => entry.situation_pack || entry.layer === 'situation_pack');
  const situationCount = layers?.situation_pack ?? situations.length;
  const branch = (layer, label, short, count, entries) => count > 0 && <div className="ui-trace-persona" role="group" aria-label={label} data-context-layer={layer} tabIndex={-1}>
    <div className="ui-trace-step"><StatusDot state="done"/><span>{short}</span><span className="ui-trace-count">{count}</span></div>
    <Citations entries={entries} onOpenCitation={onOpenCitation}/>
  </div>;
  return <ol className="ui-ladder-trace" aria-label="本次用了什么">{levels.map(([layer, label]) => {
    const step = trace.find((entry) => entry.layer === layer);
    return <li key={layer} data-context-layer={layer} tabIndex={-1} aria-label={label}>
      <div className="ui-trace-step">{step?.selected > 0 ? <StatusDot state="done"/> : <span className="ui-trace-unused" role="img" aria-label="未用到"/>}<span>{label}</span><span className="ui-trace-count">{step?.selected ?? 0}</span>{step?.stopped && <span className="ui-trace-stop" title="已足够" aria-label="已足够">—</span>}</div>
      <Citations entries={citations.filter((entry) => entry.layer === layer && !entry.persona && !entry.situation_pack)} onOpenCitation={onOpenCitation}/>
    {layer === 'insight' && <>{branch('persona', '画像', '我', personaCount, persona)}
      {branch('situation_pack', '情境', '情境', situationCount, situations)}
      {branch('inspiration', '灵感', '灵感', inspirationCount, inspirations)}</>}</li>;
  })}</ol>;
}
export default LadderTrace;
