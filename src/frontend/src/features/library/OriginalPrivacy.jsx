import { useEffect, useRef, useState } from 'react';
import { Row, Switch } from '../../shared/ui';
import { libraryApi } from './libraryApi';

export function OriginalPrivacy({ projectId, sourceId, api = libraryApi }) {
  const [state, setState] = useState(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  const generation = useRef(0);
  useEffect(() => {
    const current = ++generation.current;
    const controller = new AbortController();
    setState(null); setBusy(true); setError('');
    api.sourcePrivacy(projectId, sourceId, { signal: controller.signal })
      .then(value => { if (current === generation.current) setState(value); })
      .catch(() => { if (current === generation.current) setError('读取未完成 · 重试'); })
      .finally(() => { if (current === generation.current) setBusy(false); });
    return () => { ++generation.current; controller.abort(); };
  }, [projectId, sourceId, api]);
  async function toggle(privateState) {
    if (!state || busy || state.inherited) return;
    const current = generation.current;
    setBusy(true); setError('');
    try {
      await api.setSourcePrivacy(projectId, sourceId, state, privateState);
    } catch (failure) {
      if (current === generation.current) setError(failure.message || '保存未完成 · 重试');
    }
    try {
      const fresh = await api.sourcePrivacy(projectId, sourceId);
      if (current === generation.current) setState(fresh);
    } catch {
      if (current === generation.current) { setState(null); setError('读取未完成 · 重试'); }
    } finally {
      if (current === generation.current) setBusy(false);
    }
  }
  return <><Row readOnly title="私密" trailing={<Switch label="私密"
    checked={state?.allowed_purposes?.length === 0} disabled={busy || !state || state.inherited}
    onChange={toggle}/>}/>{error && <p role="alert">{error}</p>}</>;
}
