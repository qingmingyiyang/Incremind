import { Icon } from "./Icon";
import { CandidateHint } from './CandidateHint';
import "./memory.css";
export function InsightChip({ insight, onConfirm, onDrop }) {
  const pending = insight.state === "pending";
  const active = insight.state === "active";
  return <span className={`ui-insight-chip ${active ? "is-confirmed" : ""} ${insight.state === "forgotten" ? "is-forgotten" : ""}`}>{insight.pattern && <span role="img" aria-label="规律"><Icon name="pattern" size={14}/></span>}<span>{insight.text}</span>{pending && <><CandidateHint relation={insight.hint?.relation} commentSource={insight.comment_source}/><button type="button" aria-label="确认" title="确认" disabled={!onConfirm} onClick={() => onConfirm(insight)}><Icon name="check"/></button><button type="button" aria-label="丢弃" title="丢弃" disabled={!onDrop} onClick={() => onDrop(insight)}><Icon name="close"/></button></>}{active && <span className="ui-insight-check" role="img" aria-label="已确认"><Icon name="check"/></span>}</span>;
}
export default InsightChip;
