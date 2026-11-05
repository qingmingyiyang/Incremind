import { useEffect, useRef, useState } from 'react';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { deviceIdentity, isDeviceIdentity } from '../../shared/api/deviceTransport';
import { externalTaskCitationsApi, readTaskCitations } from './externalTaskCitationsApi';

function CitationRegistrationStatus({ data, error = false, readOnly = false }) {
  return <><p role="status">{error ? '引用登记状态待核对' : !data ? readOnly ? '引用登记状态未保存' : '正在核对引用登记' : data.state === 'recorded'
    ? data.ids.length ? '本次自动引用已登记' : '最终回复未标引用编号'
    : data.state === 'not_qualified' ? '最终回复未获证明 · 无自动登记资格'
    : data.state === 'pending' ? `待登记 ${data.ids.filter(item => !data.registered_ids.includes(item)).length} 个自动引用编号` : '引用登记结果未知'}</p>
    {data && <p>{error || data.registered_ids === null ? '已登记数量未知' : `已登记 ${data.registered_ids.length} 个引用编号`}</p>}
  </>;
}

export function ExternalTaskCitations({ value, readOnly = false }) {
  // 只读历史仅展示已保存事实，不挂载会读取或重试登记的组件。
  return readOnly ? <div aria-label="引用使用登记"><CitationRegistrationStatus data={value.state ? readTaskCitations(value) : null} readOnly/></div>
    : <ManagedExternalTaskCitations value={value}/>;
}

function ManagedExternalTaskCitations({ value }) {
  const [epoch, setEpoch] = useState(0), [result, setResult] = useState(null), [busy, setBusy] = useState(false);
  const [error, setError] = useState(false), [refresh, setRefresh] = useState(0), pending = useRef(null);
  const identity = useRef(deviceIdentity()).current;
  const gate = useRequestScope(`${value.product_id}\0${value.project_id}\0${epoch}`), scope = gate.scope;
  const data = result?.scope === scope ? result.value : value.state ? readTaskCitations(value) : null;
  useEffect(() => {
    if (!isDeviceIdentity(identity)) return undefined;
    const changed = () => { gate.invalidate('citations'); pending.current?.abort(); pending.current = null;
      setResult(null); setError(false); setBusy(false); setEpoch(old => old + 1); };
    window.addEventListener('chriptmas-user-space', changed); window.addEventListener('chriptmas-device-required', changed);
    return () => { window.removeEventListener('chriptmas-user-space', changed); window.removeEventListener('chriptmas-device-required', changed); };
  }, [gate]);
  useEffect(() => {
    if (!isDeviceIdentity(identity)) return undefined;
    const token = gate.issue('citations', scope), controller = new AbortController();
    pending.current = controller;
    setBusy(true); setError(false);
    externalTaskCitationsApi.read(value, { signal: controller.signal }).then(next => {
      if (gate.isCurrent(token) && isDeviceIdentity(identity)) setResult({ scope, value: next });
    }).catch(() => { if (gate.isCurrent(token) && isDeviceIdentity(identity)) setError(true); })
      .finally(() => { if (pending.current === controller) pending.current = null;
        if (gate.isCurrent(token) && isDeviceIdentity(identity)) setBusy(false); });
    return () => { controller.abort(); if (pending.current === controller) pending.current = null; gate.invalidate('citations'); };
  }, [scope, refresh]);
  async function retry() {
    if (busy || pending.current || !data?.ids || data.state === 'not_qualified'
        || !gate.isScopeCurrent(scope) || !isDeviceIdentity(identity)) return;
    const token = gate.issue('citations', scope), controller = new AbortController();
    pending.current = controller; setBusy(true); setError(false);
    try {
      const next = await externalTaskCitationsApi.retry(value, { signal: controller.signal });
      if (gate.isCurrent(token) && isDeviceIdentity(identity)) setResult({ scope, value: next });
    } catch { if (gate.isCurrent(token) && isDeviceIdentity(identity)) setError(true); }
    finally { if (pending.current === controller) pending.current = null;
      if (gate.isCurrent(token) && isDeviceIdentity(identity)) setBusy(false); }
  }
  if (!isDeviceIdentity(identity)) return null;
  return <div aria-label="引用使用登记"><CitationRegistrationStatus data={data} error={error}/>
    {(error || data?.state === 'unknown' || data?.state === 'pending') && <>
      <button type="button" disabled={busy} onClick={() => setRefresh(old => old + 1)}>重新核对</button>
      {data?.ids && data.state !== 'not_qualified' && <><button type="button" disabled={busy} onClick={retry}>重试引用登记</button><p>仅登记本次结果中的引用。</p></>}
    </>}
  </div>;
}
