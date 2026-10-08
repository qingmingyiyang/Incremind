import "./atoms.css";

export function Switch({ checked = false, onChange, label, disabled = false, ...props }) {
  return <button {...props} type="button" className={`ui-switch ${props.className ?? ""}`} role="switch" aria-label={label} aria-checked={checked} disabled={disabled} onClick={() => onChange?.(!checked)}><span aria-hidden="true" /></button>;
}

export default Switch;
