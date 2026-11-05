import { useEffect, useRef, useState } from 'react';
import { Icon } from '../../shared/ui';
import { userStorageKey } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';

export function ProjectScenes({ row, api, run, busy }) {
  const [scene, setScene] = useState(''), [epoch, setEpoch] = useState(0);
  const edits = useRef(0), pending = useRef(false);
  const identity = userStorageKey(row.id);
  const gate = useRequestScope(`${identity}:${epoch}`), scope = gate.scope;
  const scenes = row.scenes || [];
  const name = scene.trim(), duplicate = scenes.includes(name);
  const current = token => userStorageKey(row.id) === identity && gate.isCurrent(token);
  useEffect(() => {
    const changed = () => { gate.invalidate('write'); setEpoch(value => value + 1); };
    window.addEventListener('chriptmas-user-space', changed);
    window.addEventListener('chriptmas-device-required', changed);
    return () => {
      window.removeEventListener('chriptmas-user-space', changed);
      window.removeEventListener('chriptmas-device-required', changed);
    };
  }, [gate]);
  useEffect(() => {
    edits.current = 0; pending.current = false; setScene('');
    return () => gate.invalidate('write');
  }, [scope]);
  function save(next, submitted = null) {
    if (busy || pending.current) return;
    const token = gate.issue('write', scope);
    if (!token) return;
    pending.current = true;
    run(async () => {
      try {
        await api.saveProject(row, { scenes: next });
        if (current(token) && submitted !== null && edits.current === submitted) {
          edits.current = 0; setScene('');
        }
      } finally { if (current(token)) pending.current = false; }
    });
  }
  return <>
    <div className="settings-tags" aria-label="项目场景">{scenes.map(value => <button type="button" key={value}
      disabled={busy} aria-label={`移除场景${value}`} title="资料可从全部查看"
      onClick={() => save(scenes.filter(item => item !== value))}>{value} <Icon name="close" size={12}/></button>)}</div>
    <form onSubmit={event => { event.preventDefault(); if (name && !duplicate) save([...scenes, name], edits.current); }}>
      <label>场景<input aria-label="添加场景" value={scene} aria-invalid={duplicate || undefined}
        onChange={event => { edits.current += 1; setScene(event.target.value); }}/></label>
      <button disabled={busy || !name || duplicate}>添加</button>
    </form>
    {duplicate && <p role="status">场景已存在</p>}
  </>;
}
