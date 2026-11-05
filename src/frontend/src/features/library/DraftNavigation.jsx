import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { isDeviceIdentity } from '../../shared/api/deviceTransport';

export function useDraftNavigation({ gate, latest, owner, editor }) {
  const [navigation, setNavigation] = useState(null);
  const pendingNavigation = useRef(null), dialog = useRef(null);
  const headingId = useId(), descriptionId = useId();
  const requestNavigation = useCallback((action, options) => {
    const state = latest.current;
    if (!state || state.invalidated || !gate.isScopeCurrent(state.scope) || !isDeviceIdentity(owner.current)) return false;
    if (!state.dirty && !state.busy && !state.conflict) { action(); return true; }
    if (!pendingNavigation.current) {
      const next = { action, onCancel: options?.onCancel, scope: state.scope, identity: owner.current, focus: document.activeElement };
      pendingNavigation.current = next; setNavigation(next);
    }
    return false;
  }, [gate, latest, owner]);
  function finishNavigation(next) {
    if (!next || pendingNavigation.current !== next || !gate.isScopeCurrent(next.scope)
        || !isDeviceIdentity(next.identity) || latest.current.invalidated) return;
    pendingNavigation.current = null; setNavigation(null); dialog.current?.close(); next.action();
  }
  function cancelNavigation() {
    const next = pendingNavigation.current;
    pendingNavigation.current = null; setNavigation(null); dialog.current?.close(); next?.onCancel?.();
    if (next?.focus?.isConnected) next.focus.focus(); else editor.current?.focus();
  }
  function clearNavigation() { pendingNavigation.current = null; setNavigation(null); }
  useEffect(() => {
    const unload = event => {
      if (!latest.current?.dirty && !latest.current?.busy) return;
      event.preventDefault(); event.returnValue = '';
    };
    window.addEventListener('beforeunload', unload);
    return () => { pendingNavigation.current = null; window.removeEventListener('beforeunload', unload); };
  }, [latest]);
  useEffect(() => {
    const element = dialog.current;
    if (!navigation || !element) return;
    element.showModal();
    return () => { if (element.open) element.close(); };
  }, [navigation]);
  return { navigation, pendingNavigation, dialog, headingId, descriptionId, requestNavigation,
    finishNavigation, cancelNavigation, clearNavigation };
}

export function DraftNavigationDialog({ guard, busy, dirty, canSave, conflict, error, onSave, onDiscard }) {
  if (!guard.navigation) return null;
  return <dialog ref={guard.dialog} className="library-review-navigation-dialog" aria-labelledby={guard.headingId}
    aria-describedby={guard.descriptionId} onKeyDown={event => { if (event.key === 'Escape') event.stopPropagation(); }}
    onCancel={event => { event.preventDefault(); guard.cancelNavigation(); }}>
    <h2 id={guard.headingId}>修改尚未保存</h2><p id={guard.descriptionId}>{busy ? '操作进行中，修改仍保留。' : '保存后切换，或明确放弃本机修改。'}</p>
    {conflict}{error && <p className="library-error" role="alert">{error}</p>}
    <div className="library-actions">
      <button type="button" className="library-primary" autoFocus aria-label={dirty ? '保存后切换' : '继续切换'}
        disabled={busy || !canSave} onClick={() => dirty ? onSave() : guard.finishNavigation(guard.pendingNavigation.current)}>{dirty ? '保存切换' : '继续切换'}</button>
      <button type="button" disabled={busy} onClick={onDiscard}>放弃修改</button>
      <button type="button" onClick={guard.cancelNavigation}>取消切换</button>
    </div>
  </dialog>;
}
