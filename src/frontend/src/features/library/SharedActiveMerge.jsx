import { useEffect, useRef, useState } from 'react';
import { deviceIdentity, isDeviceIdentity, readDeviceCredential, userStorageKey } from '../../shared/api/deviceTransport';
import { readStoredJson, removeStoredItem, writeStoredJson } from '../../shared/lib/browserStorage';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { FocusPanel } from '../../shared/ui';
import { InsightMaintenance } from './LibraryTools';

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
function storage() {
  try { return globalThis.sessionStorage || null; } catch { return null; }
}
function journal(key) {
  const { value, invalid, unavailable } = readStoredJson(key, storage());
  return { operation: typeof value === 'string' && uuid.test(value) ? value : null,
    blocked: invalid || unavailable || value !== null && (typeof value !== 'string' || !uuid.test(value)) };
}

export function SharedActiveMerge({ projectId, namespace, api, detail, onClose, onDone, onBusy }) {
  const identity = deviceIdentity();
  const key = userStorageKey(`shared-active-merge:${readDeviceCredential()?.device?.device_id || ''}:${projectId}:${namespace}`);
  const gate = useRequestScope(`${key}:${identity.revision}`), scope = gate.scope;
  const [saved, setSaved] = useState(() => journal(key));
  const [checking, setChecking] = useState(false), [missing, setMissing] = useState(false);
  const [formBusy, setFormBusy] = useState(false), [readBusy, setReadBusy] = useState(false);
  const [items, setItems] = useState(null), [error, setError] = useState('');
  const [reloadTargets, setReloadTargets] = useState(0);
  const pending = useRef(false);
  const busy = formBusy || readBusy;
  const current = token => gate.isCurrent(token) && isDeviceIdentity(identity);
  const target = items?.find(row => row.id === detail?.insight.id);
  useEffect(() => { onBusy(busy); return () => onBusy(false); }, [busy, onBusy]);
  useEffect(() => {
    setItems(null); setError('');
    if (!detail) return;
    const controller = new AbortController(), token = gate.issue('targets', scope);
    api.targets(controller.signal).then(rows => {
      if (current(token)) setItems(rows);
    }).catch(reason => { if (!controller.signal.aborted && current(token)) setError(reason.message); });
    return () => { controller.abort(); gate.invalidate('targets'); };
  }, [scope, detail?.insight.id, reloadTargets]);

  function clear(operation) {
    if (journal(key).operation !== operation || !removeStoredItem(key, storage()))
      throw new Error('合并操作记录未核对');
    setSaved({ operation: null, blocked: false });
  }
  function requireResult(value, operation) {
    if (value.operation_id !== operation || value.revision !== 1
        || value.state !== 'cancelled' && (value.state !== 'applied'
          || value.result_id !== 'recognition-merge-' + operation.replaceAll('-', '')))
      throw new Error('合并结果无法核对');
  }
  async function merge({ expectedRevisions, content, conditions }) {
    const token = gate.issue('write', scope);
    if (!token || pending.current || !isDeviceIdentity(identity)) throw new Error('合并范围已变化');
    const original = journal(key), operation = crypto.randomUUID();
    if (original.blocked || original.operation || !writeStoredJson(key, operation, storage()))
      throw new Error('合并操作记录未保存');
    pending.current = true;
    setSaved({ operation, blocked: false });
    setError(''); setMissing(false);
    try {
      const value = await api.active({ operation_id: operation, expected_revisions: expectedRevisions, content, conditions });
      if (!current(token)) return null;
      requireResult(value, operation);
      if (value.state !== 'applied') throw new Error('合并结果无法核对');
      clear(operation);
      return value;
    } catch (reason) {
      if (current(token) && [400, 403, 409, 429].includes(reason.status)) clear(operation);
      throw reason;
    } finally { pending.current = false; }
  }
  async function recover(cancel) {
    if (pending.current || !saved.operation || !isDeviceIdentity(identity)) return;
    const operation = saved.operation, token = gate.issue('write', scope);
    if (!token) return;
    pending.current = true; setReadBusy(true); setMissing(false); setError('');
    try {
      const value = await (cancel ? api.cancel(projectId, operation) : api.read(projectId, operation));
      if (!current(token)) return;
      requireResult(value, operation); clear(operation); setChecking(false);
      if (value.state === 'applied') onDone(value);
    } catch (reason) {
      if (current(token)) {
        if (!cancel && reason.status === 404) setMissing(true);
        else setError(reason.message);
      }
    } finally { pending.current = false; if (current(token)) setReadBusy(false); }
  }
  async function reload() {
    const token = gate.issue('reload', scope);
    const rows = await api.targets();
    if (!current(token)) return null;
    setItems(rows);
    return { items: rows, visibleItems: rows };
  }
  return <>
    {!detail && !checking && saved.operation && <div className="library-actions">
      <button type="button" onClick={() => setChecking(true)}>核对合并</button>
    </div>}
    {(detail || checking) && <FocusPanel className="library-focus" title="合并认识" onClose={() => {
      if (!busy) { setChecking(false); setMissing(false); setError(''); onClose(); }
    }}>
      {error && <p role="alert">{error} {detail && !items &&
        <button type="button" disabled={busy} onClick={() => setReloadTargets(value => value + 1)}>重新读取</button>}</p>}
      {saved.blocked && <p role="alert">合并操作记录无法核对</p>}
      {saved.operation && <div className="library-actions">
        <button type="button" disabled={busy} onClick={() => recover(false)}>核对合并结果</button>
        {missing && <button type="button" disabled={busy} onClick={() => recover(true)}>取消未受理操作</button>}
      </div>}
      {detail && !items && !error && <p role="status">读取可合并认识</p>}
      {detail && items && !target && <p role="status">当前认识无法合并</p>}
      {target && <InsightMaintenance action="merge" projectId={projectId} insight={target} insights={items}
        merge={merge} onReload={reload} onDone={onDone} onBusy={setFormBusy}
        disabled={Boolean(saved.operation) || saved.blocked}/>}
    </FocusPanel>}
  </>;
}
