import { StatusDot } from "./StatusDot";
import "./atoms.css";

const insightFilters = [{ key: "pending", label: "待确认", dot: "pending" }, { key: "active", label: "生效", dot: "done" }, { key: "stale", label: "需复核", dot: "unverified" }, { key: "forgotten", label: "已遗忘", dot: "forgotten" }];

export function FilterBar({ value, counts = {}, onChange, filters = insightFilters, label = "认识状态" }) {
  return <div className="ui-filter-bar" role="group" aria-label={label}>
    {filters.map(({ key, label, dot }) => <button type="button" key={key} className="ui-filter" aria-label={`${label} ${counts[key] ?? 0}`} aria-pressed={value === key} onClick={() => onChange?.(key)}><span aria-hidden="true"><StatusDot state={dot} /></span><span>{label}</span><span className="ui-count">{counts[key] ?? 0}</span></button>)}
  </div>;
}

export default FilterBar;
