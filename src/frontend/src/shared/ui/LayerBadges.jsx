import "./memory.css";
const layersList = [["insight", "认", "认识"], ["summary", "摘", "摘要"], ["note", "整", "整理稿"], ["source", "原", "原件"], ["persona", "我", "我"], ["inspiration", "灵", "灵感"]];
export function LayerBadges({ layers = {}, onOpenTrace }) {
  return <div className="ui-layer-badges" aria-label="本次用了什么">{layersList.filter(([key]) => layers[key] > 0).map(([key, short, label]) => <button key={key} type="button" title={`${label} ${layers[key]}`} aria-label={`${label} ${layers[key]} · 本次用了什么`} disabled={!onOpenTrace} onClick={() => onOpenTrace(key)}>{short} {layers[key]}</button>)}</div>;
}
export default LayerBadges;
