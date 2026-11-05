import { useEffect, useRef, useState } from 'react';
import { FocusPanel, Icon, MarkdownBody, Row, StatusDot } from '../../shared/ui';
import { deviceIdentity, isDeviceIdentity } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { libraryApi } from './libraryApi';

const admissible = row => typeof row.document_id === 'string' && Boolean(row.document_id)
  && Number.isSafeInteger(row.revision) && row.revision > 0 && typeof row.title === 'string'
  && !row.archived && row.recall_state !== 'forgotten' && !row.filing;
const validDetails = value => value && typeof value.explanation === 'string' && Array.isArray(value.questions)
  && value.questions.every(question => typeof question === 'string' && Boolean(question.trim()))
  && Array.isArray(value.evidence) && value.evidence.length > 0 && value.evidence.every(evidence =>
    typeof evidence?.document_id === 'string' && Boolean(evidence.document_id)
    && Number.isSafeInteger(evidence.revision) && evidence.revision > 0 && typeof evidence.title === 'string'
    && Number.isSafeInteger(evidence.start) && Number.isSafeInteger(evidence.end)
    && evidence.start >= 0 && evidence.end > evidence.start && typeof evidence.quote === 'string' && Boolean(evidence.quote));
export const validInsight = row => typeof row?.id === 'string' && Boolean(row.id) && typeof row.text === 'string'
  && Number.isSafeInteger(row.revision) && row.revision > 0 && Array.isArray(row.conditions)
  && row.conditions.every(value => typeof value === 'string') && (!row.inspiration || validDetails(row.inspiration))
  && ['candidate', 'recognition'].includes(row.kind) && ['pending', 'active', 'stale', 'forgotten'].includes(row.state);

export function InspirationDetails({ projectId, insight, scopeKey, onOpenDocument }) {
  const details = insight?.inspiration;
  const gate = useRequestScope(`${scopeKey}\0${projectId}\0${insight?.id}\0${insight?.revision}`), scope = gate.scope;
  const [preview, setPreview] = useState(null), pending = useRef(null), identityAtOpen = useRef(deviceIdentity());
  const [invalidated, setInvalidated] = useState(false);
  useEffect(() => () => { pending.current?.abort(); }, [scope]);
  useEffect(() => {
    const changed = () => {
      pending.current?.abort(); gate.invalidate('source'); setPreview(null); setInvalidated(true);
    };
    window.addEventListener('chriptmas-user-space', changed);
    window.addEventListener('chriptmas-device-required', changed);
    return () => {
      window.removeEventListener('chriptmas-user-space', changed);
      window.removeEventListener('chriptmas-device-required', changed);
    };
  }, [gate]);
  async function readSource(evidence, ordinal) {
    if (!isDeviceIdentity(identityAtOpen.current)) return;
    pending.current?.abort();
    const token = gate.issue('source', scope);
    if (!token) return;
    const identity = deviceIdentity(), controller = new AbortController();
    pending.current = controller;
    const current = () => !controller.signal.aborted && gate.isCurrent(token) && isDeviceIdentity(identity);
    setPreview({ scope, ordinal, loading: true });
    try {
      const document = await libraryApi.documentRevision(projectId, evidence.document_id, evidence.revision, { signal: controller.signal });
      if (!current()) return;
      const characters = typeof document.markdown === 'string' ? Array.from(document.markdown) : [];
      if (document.document_id !== evidence.document_id || document.revision !== evidence.revision
        || !Number.isSafeInteger(evidence.start) || !Number.isSafeInteger(evidence.end)
        || evidence.start < 0 || evidence.end <= evidence.start || evidence.end > characters.length
        || characters.slice(evidence.start, evidence.end).join('') !== evidence.quote)
        throw new Error('inspiration_source_changed');
      setPreview({ scope, ordinal, document });
    } catch (error) {
      if (current() && error.name !== 'AbortError') setPreview({ scope, ordinal, error: true });
    }
  }
  if (!details || invalidated || !isDeviceIdentity(identityAtOpen.current)) return null;
  if (!validDetails(details)) return <p className="library-error" role="alert">灵感未读取</p>;
  const displayed = preview?.scope === scope ? preview : null;
  return <>
    {details.explanation && <p className="library-summary-text">{details.explanation}</p>}
    {details.evidence?.length > 0 && <section className="library-grown library-evidence" aria-label="灵感出处"><h3>出处</h3>
      {details.evidence.map((evidence, index) => <div key={`${evidence.document_id}:${evidence.revision}:${index}`}>
        <Row title={evidence.title} sub={evidence.quote} meta={`v${evidence.revision}`}
          onOpen={() => readSource(evidence, index)}/>
        {displayed?.ordinal === index && (displayed.loading ? <span role="status">读取中</span>
          : displayed.error ? <div className="library-error" role="alert"><span>出处未打开</span><button type="button" onClick={() => readSource(evidence, index)}>重试</button></div>
          : <section aria-label="引用时正文"><p className="library-meta">v{displayed.document.revision}</p>
            <MarkdownBody>{displayed.document.markdown}</MarkdownBody>
            {onOpenDocument && <div className="library-actions"><button type="button" onClick={() => {
              if (gate.isScopeCurrent(scope) && isDeviceIdentity(identityAtOpen.current)) onOpenDocument(evidence);
            }}>打开整理稿</button></div>}
          </section>)}
      </div>)}
    </section>}
    {details.questions?.length > 0 && <section className="library-grown" aria-label="推荐问题"><h3>问题</h3>
      <ul className="library-conditions">{details.questions.map((question, index) => <li key={index}>{question}</li>)}</ul>
    </section>}
  </>;
}

export function InspirationExtraction({ projectId, scene = null, initialDocumentId, scopeKey,
  onClose, onOpenInsight, onOpenDocument, onGenerated, onBusyChange }) {
  const gate = useRequestScope(`${scopeKey}\0${projectId}\0${scene || ''}`), scope = gate.scope;
  const [loaded, setLoaded] = useState(null), [selected, setSelected] = useState([]), [reload, setReload] = useState(0);
  const [error, setError] = useState(null), [running, setRunning] = useState(false), [result, setResult] = useState(null);
  const [invalidated, setInvalidated] = useState(false), pending = useRef(null);
  useEffect(() => { onBusyChange?.(running); }, [running, onBusyChange]);
  useEffect(() => () => onBusyChange?.(false), [onBusyChange]);
  useEffect(() => {
    const controller = new AbortController(), identity = deviceIdentity(), token = gate.issue('list', scope);
    const current = () => !controller.signal.aborted && gate.isCurrent(token) && isDeviceIdentity(identity);
    setLoaded(null); setSelected([]); setError(null); setResult(null);
    libraryApi.list('note', projectId, { scene, signal: controller.signal }).then(value => {
      if (!current()) return;
      if (!Array.isArray(value?.items)) throw new Error('invalid_inspiration_documents');
      const unique = new Map(value.items.filter(admissible).map(row => [row.document_id, row]));
      const rows = [...unique.values()];
      setLoaded({ scope, rows });
      if (initialDocumentId && unique.has(initialDocumentId)) setSelected([initialDocumentId]);
    }).catch(failure => {
      if (current() && failure.name !== 'AbortError') setLoaded({ scope, error: true });
    });
    return () => { controller.abort(); pending.current?.controller.abort(); gate.invalidate('extract'); };
  }, [scope, reload, projectId, scene, initialDocumentId]);
  useEffect(() => {
    const changed = () => {
      pending.current?.controller.abort(); gate.invalidate('list'); gate.invalidate('extract');
      setInvalidated(true); setResult(null); setLoaded(null); setSelected([]); setRunning(false);
    };
    window.addEventListener('chriptmas-user-space', changed);
    window.addEventListener('chriptmas-device-required', changed);
    return () => {
      window.removeEventListener('chriptmas-user-space', changed);
      window.removeEventListener('chriptmas-device-required', changed);
    };
  }, [gate]);
  const rows = loaded?.scope === scope ? loaded.rows : null;
  function choose(identity) {
    if (pending.current || running || invalidated) return;
    setSelected(current => current.includes(identity) ? current.filter(value => value !== identity)
      : current.length < 3 ? [...current, identity] : current);
    setError(null); setResult(null);
  }
  async function extract(retryToken) {
    if (pending.current || running || invalidated || !rows || selected.length < 1 || selected.length > 3) return;
    const documents = rows.filter(row => selected.includes(row.document_id))
      .map(row => ({ document_id: row.document_id, expected_revision: row.revision }))
      .sort((left, right) => left.document_id.localeCompare(right.document_id));
    if (documents.length !== selected.length) return;
    const token = gate.issue('extract', scope);
    if (!token) return;
    const identity = deviceIdentity(), controller = new AbortController(), operation = { controller, token };
    pending.current = operation;
    const current = () => !controller.signal.aborted && pending.current === operation
      && gate.isCurrent(token) && isDeviceIdentity(identity);
    setRunning(true); setError(null); setResult(null);
    try {
      const value = await libraryApi.extractInspirations(projectId, scene, documents, { signal: controller.signal, retryToken });
      if (!current()) return;
      if (typeof value?.turn_id !== 'string' || !value.turn_id || !Array.isArray(value.items)
        || value.items.length > 5 || value.items.some(row => !validInsight(row))) throw new Error('invalid_inspiration_result');
      setResult({ scope, items: value.items }); onGenerated?.();
    } catch (failure) {
      if (current() && failure.name !== 'AbortError') setError({ message: failure.status ? failure.message : '提炼状态未确认',
      retryToken: (failure.status === 502 && ['inspiration_invalid_output', 'inspiration_generation_failed'].includes(failure.code)
        || failure.status === 409 && failure.code === 'inspiration_material_budget_insufficient')
        && typeof failure.retryToken === 'string' && failure.retryToken ? failure.retryToken : null,
      budgetRetry: failure.status === 409 && failure.code === 'inspiration_material_budget_insufficient',
      refresh: failure.code === 'inspiration_sources_changed' || failure.status === 404,
      blocked: failure.code !== 'inspiration_material_budget_insufficient' || Boolean(failure.retryToken) });
    } finally {
      if (pending.current === operation) pending.current = null;
      if (gate.isCurrent(token)) setRunning(false);
    }
  }
  const items = result?.scope === scope ? result.items : null;
  return <FocusPanel className="library-focus" title="提炼灵感" onClose={onClose} status={running ? 'processing' : undefined}>
    {invalidated ? <p className="library-meta">空间已变化</p> : !loaded || loaded.scope !== scope ? <StatusDot state="processing"/>
      : loaded.error ? <div className="library-error" role="alert"><span>资料未读取</span><button type="button" onClick={() => setReload(value => value + 1)}>重试</button></div>
      : <>
        <p className="library-meta">{selected.length}/3</p>
        <section aria-label="选择整理稿">{rows.map(row => <Row key={row.document_id} title={row.title} selected={selected.includes(row.document_id)}
          onOpen={() => choose(row.document_id)} readOnly={running || !selected.includes(row.document_id) && selected.length >= 3}
          trailing={<label className="library-review-checkbox"><input type="checkbox" aria-label={`选择整理稿 ${row.title}`}
            checked={selected.includes(row.document_id)} disabled={running || !selected.includes(row.document_id) && selected.length >= 3}
            onChange={() => choose(row.document_id)}/><span><Icon name="check" size={12}/></span></label>}/>)}</section>
        {rows.length === 0 && <p className="library-meta">暂无可提炼资料</p>}
        {error && <div className="library-error" role="alert"><span>{error.message}</span>
          {error.refresh && <button type="button" disabled={running} onClick={() => setReload(value => value + 1)}>刷新资料</button>}
          {error.retryToken && <button type="button" disabled={running} onClick={() => extract(error.retryToken)}>{error.budgetRetry ? '重试提炼' : '重试'}</button>}
        </div>}
        <div className="library-actions"><button type="button" className="library-primary"
          disabled={running || !selected.length || Boolean(error?.blocked) || items !== null} onClick={() => extract()}>{running ? '提炼中' : '提炼灵感'}</button>
          {items !== null && <button type="button" onClick={() => { setResult(null); setError(null); }}>重新选择</button>}
        </div>
        {items?.length === 0 && <p className="library-meta">暂无灵感</p>}
        {items?.map(item => <section className="library-grown" key={item.id}>
          <Row title={item.text} dot={item.state === 'pending' ? 'pending' : item.state === 'forgotten' ? 'forgotten' : 'done'}
            onOpen={() => onOpenInsight?.(item)}/>
          {item.conditions?.length > 0 && <ul className="library-conditions">{item.conditions.map((value, index) => <li key={index}>{value}</li>)}</ul>}
          <InspirationDetails projectId={projectId} insight={item} scopeKey={`${scopeKey}:${item.id}:${item.revision}`}
            onOpenDocument={onOpenDocument}/>
        </section>)}
      </>}
  </FocusPanel>;
}
