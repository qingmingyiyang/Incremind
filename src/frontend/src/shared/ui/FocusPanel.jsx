import { useEffect, useId, useRef, useState } from "react";
import { Icon } from "./Icon";
import { StatusDot } from "./StatusDot";
import "./memory.css";
const statuses = { unverified: "未核对", done: "已核对", processing: "处理中", pending: "待确认", failed: "失败", forgotten: "已遗忘" };
export function FocusPanel({ title, status, actions, onClose, children, className = "", focusScope }) {
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
  useEffect(() => {
    // 资料层级切换可能移除当前控件；保留仍在面板内的编辑焦点。
    if (focusScope !== undefined && !panel.current?.contains(document.activeElement)) close.current?.focus();
  }, [focusScope]);
  function onKeyDown(event) {
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); onClose?.(); }
    if (event.key !== "Tab" || !overlay) return;
    const controls = [...panel.current.querySelectorAll('button,a[href],input,textarea,select,summary,[tabindex]')]
      .filter(node => {
        if (node.tabIndex < 0 || node.matches(':disabled') || node.closest('[hidden],[inert]')
          || !node.getClientRects().length || getComputedStyle(node).visibility !== 'visible') return false;
        for (let details = node.parentElement?.closest('details:not([open])'); details; details = details.parentElement?.closest('details:not([open])')) {
          if (!details.querySelector(':scope > summary')?.contains(node)) return false;
        }
        return true;
      })
      .sort((left, right) => (left.tabIndex || Infinity) - (right.tabIndex || Infinity));
    const first = controls[0], last = controls.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }
  return <aside ref={panel} role="dialog" aria-modal={overlay ? true : undefined} aria-labelledby={titleId} className={`ui-focus-panel ${className}`} onKeyDown={onKeyDown}><header><h2 id={titleId}>{title}</h2>{status && <span className="ui-focus-status"><StatusDot state={status}/>{statuses[status]}</span>}<div className="ui-focus-actions">{actions}</div><button ref={close} type="button" className="ui-focus-close" aria-label="关闭" onClick={onClose}><Icon name="close"/></button></header><div className="ui-focus-body">{children}</div></aside>;
}
export default FocusPanel;
