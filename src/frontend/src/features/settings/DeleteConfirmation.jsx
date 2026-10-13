import { useState } from 'react';

export function DeleteConfirmation({ name, label, consequence, disabled, onConfirm }) {
  const [open, setOpen] = useState(false), [confirmation, setConfirmation] = useState('');
  return <div className="settings-delete">
    <button type="button" disabled={disabled} aria-label={label} aria-expanded={open}
      onClick={() => { setOpen(!open); setConfirmation(''); }}>删除</button>
    {open && <form onSubmit={event => { event.preventDefault(); if (!disabled && confirmation === name) onConfirm(confirmation); }}>
      <p>{consequence}</p>
      <label>输入名称<input aria-label={`确认删除${name}`} value={confirmation} disabled={disabled}
        autoComplete="off" placeholder={name} onChange={event => setConfirmation(event.target.value)}/></label>
      <div className="settings-delete-actions"><button type="button" disabled={disabled} onClick={() => { setOpen(false); setConfirmation(''); }}>取消</button>
        <button type="submit" disabled={disabled || confirmation !== name}>彻底删除</button></div>
    </form>}
  </div>;
}
