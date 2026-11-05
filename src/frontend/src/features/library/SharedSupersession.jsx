import { useEffect, useRef, useState } from 'react';
import { deviceIdentity, isDeviceIdentity, readDeviceCredential, userStorageKey } from '../../shared/api/deviceTransport';
import { readStoredJson, removeStoredItem, writeStoredJson } from '../../shared/lib/browserStorage';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { FocusPanel, Icon } from '../../shared/ui';
import { sharedLibraryApi, sharedSupersessionCommand } from './sharedLibraryApi';

function storage() {
  try { return globalThis.sessionStorage || null; } catch { return null; }
}
function journal(key, namespace) {
  const { value, unavailable, invalid } = readStoredJson(key, storage());
  const valid = value === null || Object.keys(value).sort().join(',') === 'candidate_id,expected_revision,namespace,target_id,target_revision'
    && value.namespace === namespace && typeof value.candidate_id === 'string' && Boolean(value.candidate_id)
    && typeof value.target_id === 'string' && Boolean(value.target_id) && value.target_id !== value.candidate_id
    && [value.expected_revision, value.target_revision].every(revision => Number.isSafeInteger(revision) && revision > 0);
  return { command: valid ? value : null, blocked: unavailable || invalid || !valid };
}

export function SharedSupersession({ projectId, namespace, detail, checking, onBusy, onJournal, onClose, onApplied, onChanged, onNotice }) {
  const actor = deviceIdentity();
  const key = userStorageKey(`shared-supersession:${readDeviceCredential()?.device?.device_id || ''}:${projectId}:${namespace}`);
  const gate = useRequestScope(`${key}:${actor.revision}`), scope = gate.scope;
  const [saved, setSaved] = useState(() => journal(key, namespace));
  const [busy, setBusy] = useState(false);
  const [permit, setPermit] = useState(null);
  const [options, setOptions] = useState(null), [selection, setSelection] = useState(''), [reload, setReload] = useState(0);
  const [error, setError] = useState(''), [listError, setListError] = useState('');
  const pending = useRef(null), candidate = detail?.insight;
  const current = token => gate.isCurrent(token) && isDeviceIdentity(actor);
  useEffect(() => { onJournal(saved.command?.candidate_id || null); }, [saved.command?.candidate_id, onJournal]);
  useEffect(() => () => onBusy(false), [onBusy]);
  useEffect(() => { setError(''); setPermit(null); }, [candidate?.id, checking]);
  useEffect(() => {
    setOptions(null); setSelection(''); setListError('');
    if (!candidate || saved.command) return;
    const controller = new AbortController(), token = gate.issue('targets', scope);
    sharedLibraryApi.supersessionTargets(projectId, detail, { signal: controller.signal }).then(value => {
      if (current(token)) setOptions(value);
    }).catch(reason => {
      if (controller.signal.aborted || !current(token)) return;
      if (reason.status === 409) { onChanged(); onNotice(reason.message); }
      else setListError(reason.message);
    });
    return () => { controller.abort(); gate.invalidate('targets'); };
  }, [scope, candidate?.id, candidate?.revision, reload, Boolean(saved.command)]);

  const storedCommand = command => JSON.stringify(journal(key, namespace).command) === JSON.stringify(command);
  function clearCommand(command) {
    if (!storedCommand(command) || !removeStoredItem(key, storage()))
      throw new Error('取代记录未核对 · 保留浏览器资料');
    setSaved({ command: null, blocked: false });
  }
  async function run(command, send, action) {
    if (pending.current || !isDeviceIdentity(actor)) return;
    if (!storedCommand(command)) { setError('取代记录未核对 · 保留浏览器资料'); return; }
    const token = gate.issue('action', scope);
    if (!token) return;
    pending.current = token; setBusy(true); onBusy(true); setError(''); setPermit(null); onNotice('');
    try {
      const result = await send();
      if (!current(token)) return;
      if (action === 'read' && result.state === 'pending') { setPermit(result); return; }
      clearCommand(command);
      if (action === 'read' && result.state === 'changed') {
        onChanged(); onNotice('原版本已变化'); return;
      }
      onNotice('已确认为取代');
      onApplied(command.candidate_id);
    } catch (reason) {
      if (!current(token)) return;
      if (action === 'start' && [400, 401, 403, 404, 409, 429].includes(reason.status)) {
        try { clearCommand(command); }
        catch (failure) { setError(failure.message); return; }
        if (reason.status === 409) { onChanged(); onNotice(reason.message); }
      }
      setError(reason.message);
    } finally {
      if (pending.current === token) pending.current = null;
      if (current(token)) { setBusy(false); onBusy(false); }
    }
  }
  const target = options?.find(item => item.id === selection);
  function submit(event) {
    event.preventDefault();
    if (!target || pending.current || !gate.isScopeCurrent(scope) || !isDeviceIdentity(actor)) return;
    try {
      const original = journal(key, namespace);
      const command = sharedSupersessionCommand(projectId, detail, target);
      if (original.blocked || original.command || !writeStoredJson(key, command, storage()))
        throw new Error('取代记录未保存 · 重新读取');
      setSaved({ command, blocked: false });
      run(command, () => sharedLibraryApi.supersede(projectId, detail, target), 'start');
    } catch (reason) { setError(reason.message); }
  }
  if (!detail && !checking) return null;
  return <FocusPanel className="library-focus" title="确认取代" focusScope={candidate?.id || saved.command?.candidate_id}
      onClose={() => { if (!pending.current) { setError(''); onClose(); } }}>
      {error && <p className="library-error" role="alert">{error}</p>}
      {saved.command ? <>
        {permit && <p className="library-meta" role="status">原版本仍待确认</p>}
        <div className="library-actions"><button type="button" disabled={busy}
          onClick={() => run(saved.command, () => sharedLibraryApi.supersession(projectId, saved.command), 'read')}>核对结果</button>
          {permit && <button type="button" disabled={busy}
            onClick={() => run(saved.command, () => sharedLibraryApi.retrySupersession(projectId, saved.command, permit), 'retry')}>按原选择重试</button>}
        </div>
      </>
        : detail && <form className="library-grown" onSubmit={submit}>
          <p className="library-insight-text">{candidate.text}</p>
          {saved.blocked && <p className="library-error" role="alert">浏览器无法保存取代记录</p>}
          {listError ? <div className="library-error" role="alert">{listError}<button type="button" onClick={() => setReload(value => value + 1)}>重新读取</button></div>
            : options ? <label className="library-queue-target">原认识<select value={selection} onChange={event => setSelection(event.target.value)}>
              <option value="">请选择</option>{options.map(item => <option key={item.id} value={item.id}>{item.text.slice(0, 100)}</option>)}</select></label>
              : <p className="library-meta" role="status">读取已有认识</p>}
          {options?.length === 0 && <p className="library-meta">暂无可选认识</p>}
          {target && <p className="library-summary-text">{target.text}</p>}
          <div className="library-actions"><button type="submit" disabled={!target || busy || saved.blocked}>
            <Icon name="supersedes" size={16}/>确认为取代</button></div>
        </form>}
    </FocusPanel>;
}
