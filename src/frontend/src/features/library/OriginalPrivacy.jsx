import { useEffect, useState } from 'react';
import { Row, Switch } from '../../shared/ui';
import { libraryApi } from './libraryApi';
import { PRIVATE_MATERIAL_HINT } from '../../shared/lib/privacyMessages';
import { useRequestScope } from '../../shared/lib/useRequestScope';

export function OriginalPrivacy({ projectId, sourceId, api = libraryApi }) {
  const [state, setState] = useState(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const gate = useRequestScope(`${projectId}\0${sourceId}\0${attempt}`), scope = gate.scope;
  useEffect(() => {
    const changed = () => {
      gate.invalidate('privacy'); setState(null); setBusy(true); setError('');
      setAttempt(value => value + 1);
    };
    window.addEventListener('chriptmas-user-space', changed);
    window.addEventListener('chriptmas-device-required', changed);
    return () => {
      window.removeEventListener('chriptmas-user-space', changed);
      window.removeEventListener('chriptmas-device-required', changed);
    };
  }, []);
  useEffect(() => {
    const current = gate.issue('privacy', scope);
    const controller = new AbortController();
    setState(null); setBusy(true); setError('');
    api.sourcePrivacy(projectId, sourceId, { signal: controller.signal })
      .then(value => { if (gate.isCurrent(current)) setState(value); })
      .catch(() => { if (gate.isCurrent(current)) setError('读取未完成'); })
      .finally(() => { if (gate.isCurrent(current)) setBusy(false); });
    return () => { gate.invalidate('privacy'); controller.abort(); };
  }, [projectId, sourceId, api, scope]);
  async function toggle(privateState) {
    if (!state || busy || state.inherited) return;
    const current = gate.issue('privacy', scope); if (!current) return;
    setBusy(true); setError('');
    try {
      await api.setSourcePrivacy(projectId, sourceId, state, privateState);
    } catch (failure) {
      if (gate.isCurrent(current)) setError(failure.message || '保存未完成 · 重试');
    }
    if (!gate.isCurrent(current)) return;
    try {
      const fresh = await api.sourcePrivacy(projectId, sourceId);
      if (gate.isCurrent(current)) setState(fresh);
    } catch {
      if (gate.isCurrent(current)) { setState(null); setError('读取未完成'); }
    } finally {
      if (gate.isCurrent(current)) setBusy(false);
    }
  }
  return <><Row readOnly title="私密" trailing={<Switch label="私密"
    title={`${state?.inherited ? '已继承项目或来源的私密限制。' : ''}${PRIVATE_MATERIAL_HINT}`}
    checked={state?.allowed_purposes?.length === 0} disabled={busy || !state || state.inherited}
    onChange={toggle}/>}/>{error && <p role="alert">{error}{!state && <> · <button type="button" disabled={busy} onClick={() => setAttempt(value => value + 1)}>重试</button></>}</p>}</>;
}
