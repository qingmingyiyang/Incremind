import "./atoms.css";

const labels = { online: "在线", offline: "离线", processing: "处理中", pending: "待确认", done: "已沉淀", failed: "失败", unverified: "未核对", forgotten: "已遗忘", 'forgotten-user': "已遗忘", cooled: "已淡忘" };

export function StatusDot({ state = "done", className = "", ...props }) {
  return <span {...props} role="img" aria-label={labels[state]} title={labels[state]} data-state={state} className={`ui-status-dot ui-status-${state} ${className}`} />;
}

export default StatusDot;
