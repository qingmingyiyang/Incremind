import { useRef, useState } from 'react';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { libraryApi } from './libraryApi';

export function InboxFiling({ row, targetProjectId, scenes, suggestion, onFiled, onError }) {
  const [choosing, setChoosing] = useState(false), [scene, setScene] = useState(''), [busy, setBusy] = useState(false);
  const pending = useRef(false), gate = useRequestScope(`${targetProjectId}:${row.id}:${row.revision}`);
  async function file(destination) {
    if (pending.current) return;
    const request = gate.issue('file', gate.scope);
    pending.current = true; setBusy(true);
    try {
      await libraryApi.fileInbox(targetProjectId, row, destination || null);
      if (gate.isCurrent(request)) onFiled();
    } catch (error) { if (gate.isCurrent(request)) onError(error.message); }
    finally { if (gate.isCurrent(request)) { pending.current = false; setBusy(false); } }
  }
  return <div className="library-inbox-filing">
    {choosing ? <><select aria-label="归类场景" value={scene} disabled={busy} onChange={event => setScene(event.target.value)}><option value="">不设场景</option>{scenes.map(name => <option key={name} value={name}>{name}</option>)}</select><button type="button" disabled={busy} onClick={() => file(scene)}>归类</button><button type="button" disabled={busy} aria-label="取消归类" onClick={() => setChoosing(false)}>×</button></>
      : <button type="button" disabled={busy} aria-label={suggestion ? `归入场景：${suggestion}` : `选择场景：${row.text}`} onClick={() => suggestion ? file(suggestion) : setChoosing(true)}>→ {suggestion || '场景'}</button>}
  </div>;
}
