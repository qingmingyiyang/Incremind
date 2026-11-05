import { Icon } from "./Icon";
import "./memory.css";
const labels = { remember: "记住", ask: "问", do: "干活", inspiration: "灵感" };
const reminderStates = { active: "已设提醒", done: "已完成", deleted: "已取消", unverifiable: "提醒暂不可核验" };
function reminderTime(value) {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }).format(date) : null;
}
export function Receipt({ kind = "remember", children, className = "", reminder, onNavigate, ...props }) {
  const time = reminder && reminder.state !== 'unverifiable' && reminderTime(reminder.at);
  return <section {...props} className={`ui-receipt ${className}`} data-kind={kind} aria-label={reminder ? '提醒' : labels[kind]}><span className="ui-receipt-icon" aria-hidden="true"><Icon name={reminder ? 'calendar' : kind}/></span><div className="ui-receipt-content">{reminder ? <div className="ui-reminder-receipt"><span>{reminderStates[reminder.state] || reminderStates.unverifiable}</span>{time && <time dateTime={reminder.at}>{time}</time>}{onNavigate && <button type="button" onClick={() => onNavigate('companion', { project_id: reminder.project_id, tab: 'schedule', reminder_id: reminder.id })}>管理提醒</button>}</div> : children}</div></section>;
}
export default Receipt;
