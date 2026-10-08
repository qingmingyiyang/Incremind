import { useEffect, useId, useRef, useState } from "react";
import { Icon } from "./Icon";
import { StatusDot } from "./StatusDot";
import "./memory.css";
const statuses = { unverified: "未核对", done: "已核对", processing: "处理中", pending: "待确认", failed: "失败", forgotten: "已遗忘" };
export function FocusPanel({ title, status, actions, onClose, children, className = "" }) {
  const titleId = useId(), panel = useRef(null), close = useRef(null);
  const [overlay, setOverlay] = useState(() => globalThis.matchMedia?.("(max-width: 1023px)").matches ?? false);
  useEffect(() => {
    const media = globalThis.matchMedia?.("(max-width: 1023px)");
    const sync = () => setOverlay(media?.matches ?? false);
    media?.addEventListener?.("change", sync);
    return () => media?.removeEventListener?.("change", sync);
  }, []);
  useEffect(() => {
    const previous = document.activeElement;
    close.current?.focus();
    return () => { if (previous?.isConnected) previous.focus(); };
  }, []);
  function onKeyDown(event) {
    if (event.key === "Escape") { event.preventDefault(); onClose?.(); }
    if (event.key !== "Tab" || !overlay) return;
    const controls = [...panel.current.querySelectorAll('button:not(:disabled),a[href],input:not(:disabled),textarea:not(:disabled),select:not(:disabled),[tabindex="0"]')].filter((node) => !node.hidden && !node.closest('[hidden]'));
    const first = controls[0], last = controls.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }
  return <aside ref={panel} role="dialog" aria-modal={overlay ? true : undefined} aria-labelledby={titleId} className={`ui-focus-panel ${className}`} onKeyDown={onKeyDown}><header><h2 id={titleId}>{title}</h2>{status && <span className="ui-focus-status"><StatusDot state={status}/>{statuses[status]}</span>}<div className="ui-focus-actions">{actions}</div><button ref={close} type="button" className="ui-focus-close" aria-label="关闭" onClick={onClose}><Icon name="close"/></button></header><div className="ui-focus-body">{children}</div></aside>;
}
export default FocusPanel;
