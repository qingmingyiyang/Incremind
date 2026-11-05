import { useEffect, useRef, useState } from 'react';
import { FocusPanel, Row, StatusDot } from '../../shared/ui';
import { deviceIdentity, isDeviceIdentity } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { InspirationDetails, validInsight } from './InspirationExtraction';
import { libraryApi } from './libraryApi';

const runningStatuses = new Set(['running']);
const terminalStatuses = new Set(['done', 'failed', 'unknown', 'empty', 'skipped']);
const statusLabels = {
  running: '处理中', done: '已提炼', failed: '提炼未完成', unknown: '提炼状态未确认',
  empty: '暂无灵感', skipped: '本次未提炼',
};
const reasonLabels = {
  no_eligible_materials: '暂无可提炼资料', no_insights: '本次未提炼出新观点',
  generation_disabled: '生成未开启', automatic_disabled: '自动提炼已关闭', sources_changed: '来源已变化',
  material_budget_insufficient: '材料超出当前模型容量', generation_failed: '提炼未完成',
  generation_unknown: '发送状态尚未核验', generation_evidence_unavailable: '生成证明尚未核验',
  automatic_interrupted: '自动提炼已中断', generation_unavailable: '生成暂不可用',
};

function validResult(value, projectId, jobId) {
  return value && value.job_id === jobId && value.project_id === projectId
    && (value.scene === null || typeof value.scene === 'string') && typeof value.date === 'string'
    && /^\d{4}-\d{2}-\d{2}$/.test(value.date) && (runningStatuses.has(value.status) || terminalStatuses.has(value.status))
    && typeof value.message === 'string' && (value.reason === null || typeof value.reason === 'string')
    && Number.isSafeInteger(value.revision) && value.revision > 0
    && typeof value.attention === 'boolean'
    && Number.isSafeInteger(value.unread_count) && value.unread_count >= 0
    && Array.isArray(value.items) && value.items.length <= 5 && value.items.every(validInsight)
    && new Set(value.items.map(item => item.id)).size === value.items.length;
}

export function AutomaticInspirations({ projectId, jobId, scopeKey, onClose, onOpenInsight, onOpenDocument, onRead }) {
  const gate = useRequestScope(`${scopeKey}\0${projectId}\0${jobId}`), scope = gate.scope;
  const [loaded, setLoaded] = useState(null), [reading, setReading] = useState(true), [loadError, setLoadError] = useState(false);
  const [reload, setReload] = useState(0), [acknowledgement, setAcknowledgement] = useState(null);
  const [invalidated, setInvalidated] = useState(false);
  const [visible, setVisible] = useState(() => document.visibilityState === 'visible');
  const readRequest = useRef(null), acknowledgeRequest = useRef(null), attemptedAcknowledgement = useRef(null);
  const onReadRef = useRef(onRead); onReadRef.current = onRead;

  useEffect(() => {
    const changed = () => setVisible(document.visibilityState === 'visible');
    document.addEventListener('visibilitychange', changed);
    return () => document.removeEventListener('visibilitychange', changed);
  }, []);

  useEffect(() => {
    const changed = () => {
      readRequest.current?.abort(); acknowledgeRequest.current?.abort();
      gate.invalidate('read'); gate.invalidate('acknowledge');
      setInvalidated(true); setLoaded(null); setReading(false); setAcknowledgement(null);
    };
    window.addEventListener('chriptmas-user-space', changed);
    window.addEventListener('chriptmas-device-required', changed);
    return () => {
      window.removeEventListener('chriptmas-user-space', changed);
      window.removeEventListener('chriptmas-device-required', changed);
      readRequest.current?.abort(); acknowledgeRequest.current?.abort();
    };
  }, [gate]);

  useEffect(() => {
    if (invalidated) return;
    const controller = new AbortController(), identity = deviceIdentity();
    readRequest.current = controller; acknowledgeRequest.current?.abort();
    gate.invalidate('acknowledge'); attemptedAcknowledgement.current = null;
    setReading(true); setLoadError(false); setAcknowledgement(null);
    let timer, polls = 0, failures = 0;
    const alive = () => !controller.signal.aborted && gate.isScopeCurrent(scope) && isDeviceIdentity(identity);
    async function read() {
      const token = gate.issue('read', scope);
      if (!token || !alive()) return;
      try {
        const value = await libraryApi.automaticInspirations(projectId, jobId, { signal: controller.signal });
        if (!alive() || !gate.isCurrent(token)) return;
        if (!validResult(value, projectId, jobId)) throw new Error('invalid_automatic_inspirations');
        setLoaded({ scope, value, identity }); setReading(false); setLoadError(false); failures = 0;
        if (runningStatuses.has(value.status) && polls < 60) {
          polls += 1; timer = setTimeout(read, Math.min(15000, 1000 * 2 ** Math.min(polls, 4)));
        }
      } catch (failure) {
        if (!alive() || !gate.isCurrent(token) || failure.name === 'AbortError') return;
        setReading(false); setLoadError(true);
        if (++failures < 3) timer = setTimeout(read, Math.min(15000, 2000 * 2 ** failures));
      }
    }
    read();
    return () => { controller.abort(); clearTimeout(timer); acknowledgeRequest.current?.abort(); gate.invalidate('acknowledge'); };
  }, [scope, reload, projectId, jobId, invalidated]);

  const displayed = loaded?.scope === scope && isDeviceIdentity(loaded.identity) && !invalidated ? loaded : null;
  useEffect(() => {
    const value = displayed?.value;
    if (!value || !visible || reading || loadError || !terminalStatuses.has(value.status) || !value.attention) return;
    const key = `${jobId}:${value.revision}`;
    if (attemptedAcknowledgement.current === key) return;
    const token = gate.issue('acknowledge', scope);
    if (!token) return;
    const identity = displayed.identity, controller = new AbortController();
    attemptedAcknowledgement.current = key; acknowledgeRequest.current = controller;
    const current = () => !controller.signal.aborted && gate.isCurrent(token) && isDeviceIdentity(identity);
    // 此 effect 在已成功显示完整结果后记录已读，认识状态由原审阅操作改变。
    libraryApi.readAutomaticInspirations(projectId, jobId, value.revision, { signal: controller.signal }).then(() => {
      if (current()) { setAcknowledgement({ scope, done: true }); onReadRef.current?.(); }
    }).catch(failure => {
      if (current() && failure.name !== 'AbortError') setAcknowledgement({ scope, conflict: failure.status === 409, error: true });
    });
  }, [displayed, visible, reading, loadError, gate, scope, projectId, jobId]);

  const value = displayed?.value, ack = acknowledgement?.scope === scope ? acknowledgement : null;
  const current = () => displayed && gate.isScopeCurrent(scope) && isDeviceIdentity(displayed.identity) && !invalidated;
  const pending = value?.items.some(row => row.state === 'pending');
  const failed = value && ['failed', 'unknown'].includes(value.status);
  const status = value ? runningStatuses.has(value.status) ? 'processing' : failed ? 'failed'
    : pending ? 'pending' : value.status === 'done' ? 'done' : undefined : undefined;
  return <FocusPanel className="library-focus" title="每日灵感" status={status} onClose={onClose}>
    {invalidated ? <p className="library-meta">空间已变化</p> : <>
      {!value && reading && <StatusDot state="processing"/>}
      {loadError && <div className="library-error" role="alert"><span>灵感未读取</span><button type="button" onClick={() => setReload(current => current + 1)}>重试读取</button></div>}
      {value && <>
        <p className="library-meta"><time dateTime={value.date}>{value.date}</time>{value.scene ? ` · ${value.scene}` : ''}</p>
        <p className="library-meta" role={failed ? 'alert' : 'status'}>{value.message || reasonLabels[value.reason] || statusLabels[value.status]}</p>
        {runningStatuses.has(value.status) && <button type="button" disabled={reading} onClick={() => setReload(current => current + 1)}>刷新</button>}
        {value.items.length === 0 && !runningStatuses.has(value.status) && !failed && <p className="library-meta">暂无灵感</p>}
        {value.items.map(item => <section className="library-grown" key={item.id}>
          <Row title={item.text} dot={item.state === 'pending' ? 'pending' : item.state === 'forgotten' ? 'forgotten' : 'done'}
            onOpen={() => { if (current()) onOpenInsight?.(item); }}/>
          {item.conditions.length > 0 && <ul className="library-conditions">{item.conditions.map((condition, index) => <li key={index}>{condition}</li>)}</ul>}
          <InspirationDetails projectId={projectId} insight={item} scopeKey={`${scopeKey}:${jobId}:${value.revision}:${item.id}:${item.revision}`}
            onOpenDocument={evidence => { if (current()) onOpenDocument?.(evidence); }}/>
        </section>)}
        {ack?.error && <div className="library-error" role="alert"><span>{ack.conflict ? '灵感已变化' : '已读未保存'}</span>
          <button type="button" onClick={() => setReload(current => current + 1)}>重新读取</button></div>}
      </>}
    </>}
  </FocusPanel>;
}
