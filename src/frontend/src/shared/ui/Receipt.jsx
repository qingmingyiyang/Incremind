import { Icon } from "./Icon";
import "./memory.css";
const labels = { remember: "记住", ask: "问", do: "干活", inspiration: "灵感" };
export function Receipt({ kind = "remember", children, className = "", ...props }) {
  return <section {...props} className={`ui-receipt ${className}`} data-kind={kind} aria-label={labels[kind]}><span className="ui-receipt-icon" aria-hidden="true"><Icon name={kind}/></span><div className="ui-receipt-content">{children}</div></section>;
}
export default Receipt;
