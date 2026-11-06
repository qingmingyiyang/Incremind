import { useId } from 'react';
import { StatusDot } from "./StatusDot";
import "./atoms.css";

export function Row({ dot, title, sub, meta, label, selected = false, expanded, onOpen, trailing, readOnly = false, className = "" }) {
  const subId = useId();
  const description = label && sub ? subId : undefined;
  const content = <><span className="ui-row-title">{title}</span>{sub && <span className="ui-row-sub" id={description}>{sub}</span>}</>;
  return <div className={`ui-row ${selected ? "is-selected" : ""} ${typeof dot === "string" && dot.startsWith("forgotten") ? "ui-row-forgotten" : ""} ${className}`}>
    {typeof dot === "string" ? <StatusDot state={dot} /> : dot}
    {readOnly ? <span className="ui-row-open">{content}</span> : <button type="button" className="ui-row-open" aria-label={label ?? (typeof title === "string" ? `${title}${sub ? ` ${sub}` : ""}` : undefined)} aria-describedby={description} aria-pressed={selected} aria-expanded={expanded} onClick={onOpen}>{content}</button>}
    {trailing}{meta && <span className="ui-row-meta">{meta}</span>}
  </div>;
}

export default Row;
