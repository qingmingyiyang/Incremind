import { useEffect, useRef, useState } from 'react';
import { userStorageKey } from '../../shared/api/deviceTransport';
import { pendingProviderOperations, PROVIDER_OPERATION_EVENT } from './providerOperationJournal';

export function useProviderOperations() {
  const scope = userStorageKey('provider-operations-v1');
  const current = useRef(scope); current.current = scope;
  const [state, setState] = useState({ scope, commands: [], error: '', ready: false });
  function refresh() {
    if (current.current !== scope) return;
    try { setState({ scope, commands: pendingProviderOperations(scope), error: '', ready: true }); }
    catch (reason) { setState({ scope, commands: [], error: reason.message, ready: false }); }
  }
  useEffect(() => {
    refresh();
    const changed = event => { if (!event.key || event.key.startsWith(scope)) refresh(); };
    window.addEventListener('storage', changed); window.addEventListener(PROVIDER_OPERATION_EVENT, changed);
    return () => { window.removeEventListener('storage', changed); window.removeEventListener(PROVIDER_OPERATION_EVENT, changed); };
  }, [scope]);
  return { scope, ...(state.scope === scope ? state : { commands: [], error: '', ready: false }), refresh };
}
