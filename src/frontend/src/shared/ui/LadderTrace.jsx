import { StatusDot } from "./StatusDot";
import "./memory.css";
const levels = [["insight", "认识"], ["summary", "摘要"], ["note", "整理稿"], ["source", "原件"]];
function Citations({ entries, onOpenCitation }) {
  return entries.length > 0 && <ul className="ui-trace-citations">{entries.map((entry) => <li key={`${entry.n}:${entry.id}`}>{onOpenCitation ? <button className="ui-trace-citation" type="button" onClick={() => onOpenCitation(entry)} aria-label={`${entry.title} · ${entry.quote}`}><span>{entry.title}</span><span>{entry.quote}</span></button> : <div className="ui-trace-citation"><span title={entry.title}>{entry.title}</span><span title={entry.quote}>{entry.quote}</span></div>}</li>)}</ul>;
}
export function LadderTrace({ trace = [], citations = [], layers, onOpenCitation }) {
  const persona = citations.filter((entry) => entry.persona);
  const personaCount = layers?.persona ?? persona.length;
  const inspirations = citations.filter((entry) => entry.layer === 'inspiration');
  const inspirationCount = layers?.inspiration ?? inspirations.length;
  const inspirationBranch = inspirationCount > 0 && <div className="ui-trace-persona" role="group" aria-label="灵感"><div className="ui-trace-step"><StatusDot state="done"/><span>灵</span><span className="ui-trace-count">{inspirationCount}</span></div><Citations entries={inspirations} onOpenCitation={onOpenCitation}/></div>;
  return <ol className="ui-ladder-trace" aria-label="本次用了什么">{levels.map(([layer, label]) => {
    const step = trace.find((entry) => entry.layer === layer);
    return <li key={layer} aria-label={label}><div className="ui-trace-step">{step?.selected > 0 ? <StatusDot state="done"/> : <span className="ui-trace-unused" role="img" aria-label="未用到"/>}<span>{label}</span><span className="ui-trace-count">{step?.selected ?? 0}</span>{step?.stopped && <span className="ui-trace-stop" title="已足够" aria-label="已足够">—</span>}</div><Citations entries={citations.filter((entry) => entry.layer === layer && !entry.persona)} onOpenCitation={onOpenCitation}/>{layer === "insight" && personaCount > 0 && <div className="ui-trace-persona" role="group" aria-label="画像"><div className="ui-trace-step"><StatusDot state="done"/><span>我</span><span className="ui-trace-count">{personaCount}</span></div><Citations entries={persona} onOpenCitation={onOpenCitation}/></div>}{layer === "insight" && inspirationBranch}</li>;
  })}</ol>;
}
export default LadderTrace;
