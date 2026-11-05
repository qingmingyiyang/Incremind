import { useEffect, useRef, useState } from 'react';
import { Row } from '../../shared/ui';
import { userStorageKey } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { workbenchApi, workbenchErrorMessage } from './workbenchApi';

const userScope = () => userStorageKey('workbench-lexicon-action');
const actionNames = { add: '添加', rename: '修改词语', set_weight: '调整权重', retire: '停用', restore: '恢复' };
const describe = value => value ? `${value.status === 'active' ? '启用' : '停用'} · 权重 ${value.weight}` : '未收录';

export function LexiconChangeReceipt({ projectId, turnId, task, onUpdated }) {
  const [user, setUser] = useState(userScope);
  const preview = task.lexicon_preview, result = task.lexicon_result;
  const gate = useRequestScope(`${user}:${projectId}:${turnId}:${preview?.preview_id || ''}:${preview?.preview_revision || ''}:${preview?.event_id || ''}:${preview?.sequence || ''}`), scope = gate.scope;
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const controller = useRef(null), pendingRequest = useRef(false);
  useEffect(() => {
    const changed = () => setUser(userScope());
    window.addEventListener('chriptmas-user-space', changed);
    window.addEventListener('chriptmas-device-required', changed);
    return () => {
      window.removeEventListener('chriptmas-user-space', changed);
      window.removeEventListener('chriptmas-device-required', changed);
    };
  }, []);
  useEffect(() => {
    pendingRequest.current = false; setBusy(false); setError('');
    return () => { controller.current?.abort(); gate.invalidate('action'); };
  }, [scope]);
  const current = token => userScope() === user && gate.isCurrent(token);
  async function decide(decision) {
    if (pendingRequest.current || busy || !preview || preview.state !== 'pending' || task.state !== 'waiting_approval') return;
    pendingRequest.current = true;
    const token = gate.issue('action', scope), request = new AbortController();
    controller.current?.abort(); controller.current = request;
    setBusy(true); setError('');
    const proposal = Object.fromEntries(['preview_id', 'preview_revision', 'event_id', 'sequence'].map(key => [key, preview[key]]));
    try {
      const value = await workbenchApi.decideLexicon(projectId, turnId, { ...proposal, decision }, { signal: request.signal });
      if (current(token)) onUpdated(value);
    } catch (failure) {
      if (current(token) && failure.name !== 'AbortError') setError(workbenchErrorMessage(failure));
    } finally {
      if (current(token)) { pendingRequest.current = false; setBusy(false); }
    }
  }
  const relevantError = typeof task.error === 'string' && /^(asr_lexicon_|agent_data_|invalid_asr_lexicon_)/.test(task.error)
    ? workbenchErrorMessage(task.error) : '';
  if (!preview && !result && !relevantError) return null;
  const source = result || preview;
  const state = result?.state || preview?.state;
  const pending = state === 'pending' && task.state === 'waiting_approval';
  const heading = state === 'saved' ? `热词表 · 已保存 · 修订 ${result.revision}`
    : state === 'read' ? `热词表 · 修订 ${result.revision}`
    : state === 'unchanged' ? `热词表 · 无需修改 · 修订 ${result.revision}`
    : state === 'cancelled' ? '热词表 · 已取消' : '热词表 · 变更预览';
  return <section className="workbench-lexicon-change" aria-label="热词表变更" aria-busy={busy}>
    <div className="workbench-lexicon-actions"><strong>{heading}</strong>{pending && <span>
      <button type="button" disabled={busy || preview.stale} onClick={() => decide('approve')}>{busy ? '处理中' : '确认保存'}</button>
      <button type="button" disabled={busy} onClick={() => decide('cancel')}>取消</button>
    </span>}</div>
    {pending && preview.stale && <div role="alert" className="workbench-error">热词表已变化 · 取消后重新提出修改</div>}
    {(source?.changes || []).map((change, index) => <Row key={index} readOnly
      title={`${actionNames[change.action]} · ${change.before?.term || change.after.term}${change.action === 'rename' ? ` → ${change.after.term}` : ''}`}
      sub={`${describe(change.before)} → ${describe(change.after)}`}/>)}
    {(result?.terms || []).map(term => <Row key={term.id} readOnly title={term.term} sub={describe(term)}/>)}
    {(error || relevantError) && <div role="alert" className="workbench-error">{error || relevantError}</div>}
  </section>;
}
