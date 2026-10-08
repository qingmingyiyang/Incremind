import "./atoms.css";

export const LAYERS = [{ key: "insight", label: "认识" }, { key: "summary", label: "摘要" }, { key: "note", label: "整理稿" }, { key: "source", label: "原件" }];

export function LayerTabs({ value, counts = {}, onChange, children }) {
  return <div className="ui-layer-tabs" role="group" aria-label="资料层级">
    {LAYERS.map(({ key, label }) => <button type="button" key={key} aria-label={`${label} ${counts[key] === null ? '—' : counts[key] ?? 0}`} aria-pressed={value === key} className="ui-layer-tab" onClick={() => onChange?.(key)}><span>{label}</span><span className="ui-count">{counts[key] === null ? '—' : counts[key] ?? 0}</span></button>)}
    {children}
  </div>;
}

export default LayerTabs;
