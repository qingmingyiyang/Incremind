import { useEffect, useRef, useState } from 'react';
import { StatusDot, Row } from '../../shared/ui';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { OutcomeFreshness } from '../../shared/ui/OutcomeFreshness';
import { deviceIdentity, isDeviceIdentity, userStorageKey } from '../../shared/api/deviceTransport';
import { libraryApi } from '../library/libraryApi';
import { readOutcomeChanges, outcomeChangeReasons } from './outcomeSources';

export function OutcomeSourceChanges({ projectId, documentId, canUpdate, onUpdate, onChanged, pendingUpdate = false }) {
  const gate = useRequestScope(`${userStorageKey('outcome-sources')}:${projectId}:${documentId}`), scope = gate.scope;
  const [result, setResult] = useState(null), [error, setError] = useState(null), [operation, setOperation] = useState(null);
  const acting = useRef(false);
  const data = result?.scope === scope && isDeviceIdentity(result.identity) ? result.data : null;
  const busy = operation?.scope === scope;
  async function read() {
    const token = gate.issue('changes', scope);
    const identity = deviceIdentity(), current = () => gate.isCurrent(token) && isDeviceIdentity(identity);
    setOperation({ scope, kind: 'read' }); setError(null);
    try {
      const value = readOutcomeChanges(await libraryApi.outcomeChanges(projectId, documentId));
      if (current()) setResult({ scope, identity, data: value });
    } catch { if (current()) setError({ scope, text: '依据变化未读取' }); }
    finally { if (current()) setOperation(null); }
  }
  useEffect(() => { read(); return () => gate.invalidate('changes'); }, [scope]);
  async function act(kind) {
    if (!data || !isDeviceIdentity(result.identity) || busy || acting.current) return;
    acting.current = true;
    const token = gate.issue('changes', scope);
    const identity = deviceIdentity(), current = () => gate.isCurrent(token) && isDeviceIdentity(identity);
    setOperation({ scope, kind }); setError(null);
    try {
      if (kind === 'update') {
        const accepted = await onUpdate(data);
        if (accepted !== true && current()) setError({ scope, text: accepted?.pending
          ? '更新结果待核对，点击继续原更新' : '更新未受理，请查看工作台提示' });
      } else {
        const value = readOutcomeChanges(await libraryApi.ackOutcomeChanges(projectId, documentId, data));
        if (current()) { setResult({ scope, identity, data: value }); onChanged?.(); }
      }
    } catch (failure) { if (current()) setError({ scope, text: failure.status === 409
      ? '内容已变化，请重新读取' : failure.status >= 400 && failure.status < 500
        ? '操作未受理，请重新读取' : '结果待核对，请重新读取当前状态' }); }
    finally { acting.current = false; if (current()) setOperation(null); }
  }
  return <section className="workbench-outcome-sources" aria-label="成果依据变化">
    <div className="workbench-intervention-heading"><span>依据变化</span>{data && <OutcomeFreshness stale={data.stale} freshCount={data.fresh_count}/>}
      <button type="button" className="ui-filter" disabled={busy} onClick={read}>重新读取</button></div>
    {busy && <StatusDot state="processing"/>}
    {data && <>
      {data.changed.map(item => <Row key={`${item.project_id}:${item.kind}:${item.id}`} title={item.title || '资料'} readOnly meta={[
        item.from_revision !== item.to_revision ? `${item.from_revision} → ${item.to_revision ?? '不可核对'}` : null,
        ...item.reasons.map(reason => outcomeChangeReasons[reason]),
      ].filter(Boolean).join(' · ')}/>)}
      {data.fresh.map(item => <Row key={item.document_id} title={item.title} readOnly meta="可补充"/>)}
      {(!data.verified || data.unverifiable.length > 0) && <p role="status">部分依据无法核对</p>}
      {!data.stale && data.fresh_count === 0 && data.verified && <span className="ui-count">当前依据未变化</span>}
      {(data.stale || data.fresh.length > 0 || pendingUpdate) && <div className="workbench-intervention-actions">
        <button type="button" disabled={busy || !canUpdate || !pendingUpdate && (!data.verified || data.unverifiable.length > 0)} onClick={() => act('update')}>{pendingUpdate ? '继续原更新' : '更新'}</button>
        <button type="button" disabled={busy || pendingUpdate} onClick={() => act('ack')}>不用</button>
      </div>}
    </>}
    {error?.scope === scope && <p role="alert">{error.text}</p>}
  </section>;
}
