import { LAYERS } from "./LayerTabs";
import "./atoms.css";

export function Breadcrumb({ levels = LAYERS, current, onJump }) {
  return <nav className="ui-breadcrumb" aria-label="下钻层级">{levels.map((level, i) => {
    const key = typeof level === "string" ? level : level.key;
    const label = typeof level === "string" ? LAYERS.find(item => item.key === key)?.label ?? level : level.label;
    return <span key={key}>{i > 0 && <span aria-hidden="true" className="ui-breadcrumb-separator">›</span>}<button type="button" aria-current={current === key ? "step" : undefined} onClick={() => onJump?.(key)}>{label}</button></span>;
  })}</nav>;
}

export default Breadcrumb;
