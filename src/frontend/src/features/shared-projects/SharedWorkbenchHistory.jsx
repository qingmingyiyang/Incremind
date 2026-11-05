import { useEffect, useMemo, useRef, useState } from 'react';
import { Composer } from '../../shared/ui/Composer';
import { Receipt } from '../../shared/ui/Receipt';
import { CitedAnswer } from '../../shared/ui/CitedAnswer';
import { StatusDot } from '../../shared/ui/StatusDot';
import { InsightChip } from '../../shared/ui/InsightChip';
import { ProgressDots } from '../../shared/ui/ProgressDots';
import { FocusPanel } from '../../shared/ui/FocusPanel';
import { MarkdownBody } from '../../shared/ui/MarkdownBody';
import { displayScopedText } from '../../shared/lib/scopeTag';
import { remoteRestriction } from '../../shared/lib/privacyMessages';
import { deviceIdentity, isDeviceIdentity, userStorageKey } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { ExternalTaskProgress } from '../workbench/ExternalTaskProgress';
import { GroupedRememberReceipt } from '../workbench/GroupedRememberReceipt';
import { RemoteConversationShare } from '../conversation-shares/RemoteConversationShare';
import { TaskDivision } from '../workbench/TaskDivision';
import { workbenchApi, workbenchErrorMessage } from '../workbench/workbenchApi';
import { sharedTaskOutcome } from '../workbench/workbenchScope';
import { sharedHistoryThread, sharedHistoryDetail } from './sharedScopeViews';

export function canStopSharedReceipt(turn) {
  const task = turn.receipt.do;
  return turn.scope_kind === 'shared' && turn.intent === 'do' && Boolean(task?.kernel_turn_id)
    && (!task.executor || task.executor === 'builtin')
    && ['researching', 'preparing', 'running', 'waiting_approval'].includes(task.state);
}

function SharedRememberReceipt({ memory, turn, actions }) {
  const shared = turn.scope_kind === 'shared';
  const readable = shared && memory.sources_available === true && actions?.canRead;
  const linked = readable && actions.onNavigate;
  const navigate = (layer, location) => actions.onNavigate('library', {
    project_id: actions.projectId, scope_kind: 'shared', layer, ...location,
  });
  const progress = memory.progress;
  const restriction = remoteRestriction(memory.error);
  const settings = restriction?.section || (memory.error === 'model_not_configured' ? 'model' : null);
  return <>
    <div className="workbench-receipt-title"><StatusDot state={memory.state}/><span>{memory.title}</span>
      {Number.isSafeInteger(progress?.done) && Number.isSafeInteger(progress?.total) && progress.total >= 0 && progress.total <= 4
        && <ProgressDots {...progress} running={memory.state === 'processing'} title="原件 · 正文 · 整理 · 入库"/>}
      {linked && memory.document_id && <button type="button" onClick={() => navigate('note', { document_id: memory.document_id })}>打开整理稿</button>}
      {linked && memory.document_id && memory.item_id && <button type="button" onClick={() => navigate('source', { source_id: memory.item_id })}>打开原件</button>}
      {shared && memory.state === 'failed' && actions?.canRetry && <button type="button"
        disabled={actions.disabled} onClick={() => actions.retry(turn)}>重试</button>}
    </div>
    {(shared && !readable ? [] : memory.insights || []).map(insight => linked
      ? <button type="button" key={insight.id} className="workbench-receipt-title"
        onClick={() => navigate('insight', { insight_id: insight.id, ...(insight.state === 'pending' ? { filter: 'pending_memory' } : {}) })}>
        <StatusDot state={insight.state === 'pending' ? 'pending' : 'done'}/>{insight.text}
      </button>
      : <span key={insight.id} className="ui-insight-chip">{insight.text}</span>)}
    {memory.error && <p role="status">
      {restriction?.reason || (settings === 'model' ? '整理模型未配置' : workbenchErrorMessage(memory.error, '原处理未完成'))}
      {settings && actions?.openSettings && <button type="button" onClick={() => actions.openSettings(settings)}>查看设置</button>}
    </p>}
  </>;
}

function SharedTaskOutcome({ projectId, turnId, documentId, historyScope }) {
  const actor = deviceIdentity(), account = userStorageKey('shared-task-outcome');
  const binding = useMemo(() => ({ account, projectId, turnId, documentId, historyScope, revision: actor.revision }),
    [account, projectId, turnId, documentId, historyScope, actor.revision]);
  const gate = useRequestScope(binding), scope = gate.scope;
  const active = useRef(null), [panel, setPanel] = useState(null);
  const visible = panel?.scope === scope && isDeviceIdentity(actor) ? panel : null;
  useEffect(() => () => { active.current?.abort(); gate.invalidate('read'); }, [scope]);
  function close() {
    active.current?.abort(); gate.invalidate('read'); setPanel(null);
  }
  async function open() {
    if (!isDeviceIdentity(actor)) return;
    const token = gate.issue('read', scope);
    if (!token) return;
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    const current = () => gate.isCurrent(token) && isDeviceIdentity(actor);
    setPanel({ scope, loading: true });
    try {
      const result = await workbenchApi.sharedTaskOutcome(projectId, turnId, { signal: controller.signal });
      if (current()) setPanel({ scope, document: sharedTaskOutcome(result, projectId, turnId, documentId) });
    } catch (failure) {
      if (current()) setPanel({ scope, error: workbenchErrorMessage(failure, '成果未打开') });
    } finally {
      if (active.current === controller) active.current = null;
    }
  }
  return <><button type="button" onClick={open}>打开成果</button>
    {visible && <FocusPanel title="成果" onClose={close}>
      {visible.loading ? <StatusDot state="processing"/> : visible.error
        ? <p role="alert">{visible.error} <button type="button" onClick={open}>重试</button></p>
        : <><h3>{visible.document.title}</h3><MarkdownBody>{visible.document.markdown}</MarkdownBody></>}
    </FocusPanel>}
  </>;
}

function SharedTaskRedoAction({ task, turn, actions, completed }) {
  const { projectId, scope: historyScope, canDo, disabled } = actions;
  const actor = deviceIdentity(), account = userStorageKey('shared-redo');
  const binding = useMemo(() => ({ account, projectId, turnId: turn.id, threadId: turn.thread_id,
    historyScope, revision: actor.revision }), [account, projectId, turn.id, turn.thread_id, historyScope, actor.revision]);
  const gate = useRequestScope(binding), scope = gate.scope;
  const active = useRef(null), [state, setState] = useState(null);
  const visible = state?.scope === scope ? state : null;
  useEffect(() => () => { active.current?.abort(); active.current = null; }, [scope]);
  if (!actor.key || !turn.thread_id) return null;
  let command;
  try { command = workbenchApi.pendingSharedRedo(projectId, turn.id); }
  catch (error) { return <span role="alert">{error.message}</span>; }
  const status = visible?.id === command?.id ? visible : null;
  async function operate(mode) {
    if (disabled || active.current || !isDeviceIdentity(actor)
        || mode === 'continue' && (!canDo || status?.state !== 'not_received')) return;
    const token = gate.issue('redo', scope);
    if (!token) return;
    const controller = new AbortController();
    active.current = controller;
    const current = () => gate.isCurrent(token) && isDeviceIdentity(actor);
    setState({ scope, id: command.id, state: 'unknown', busy: true });
    try {
      const proof = mode === 'check'
        ? await workbenchApi.checkSharedRedo(command, turn.thread_id, current, { signal: controller.signal })
        : await actions.redo(turn, undefined, command);
      if (!current() || !proof) return;
      if (proof.state === 'accepted') actions.acceptRedo(proof.result);
      if (proof.state === 'accepted' || proof.state === 'failed') workbenchApi.settleSharedRedo(command);
      setState({ scope, id: command.id, state: proof.state, error: proof.error });
    } catch (error) {
      if (current()) setState({ scope, id: command.id, state: 'unknown', message: workbenchErrorMessage(error) });
    } finally {
      if (active.current === controller) active.current = null;
      if (current()) setState(value => value?.scope === scope && value.id === command.id
        ? { ...value, busy: false } : value);
    }
  }
  if (!command) return <>
    {visible?.state === 'failed' && <span role="status">{workbenchErrorMessage(visible.error, '原重做未完成')}</span>}
    {canDo && completed && !task.interruption && !task.data_operation
      && <div className="workbench-receipt-title"><button type="button" disabled={disabled} onClick={() => actions.redo(turn)}>重做</button></div>}
    {canDo && task.state === 'failed' && actions.renderRetry(turn)}
  </>;
  const message = status?.message || (status?.state === 'not_received' ? '原重做未受理'
    : status?.state === 'preparing' ? '原重做正在准备'
    : status?.error === 'turn_interrupted' ? '原重做中断 · 待核对' : '原重做结果待核对');
  return <div className="workbench-error">
    <span role="status">{message}</span>
    <button type="button" disabled={disabled || status?.busy} onClick={() => operate('check')}>核对原重做</button>
    {canDo && status?.state === 'not_received' && <button type="button" disabled={disabled || status?.busy}
      onClick={() => operate('continue')}>继续原重做</button>}
  </div>;
}

function SharedTaskReceipt({ task, turn, actions, streamText, streamFeedback, stopAction }) {
  const running = ['researching', 'preparing', 'running'].includes(task.state);
  const completed = ['done', 'partial'].includes(task.state) && typeof task.document_id === 'string' && task.document_id;
  const builtin = turn.scope_kind === 'shared' && task.kernel_turn_id && (!task.executor || task.executor === 'builtin');
  const progress = task.progress;
  const partial = streamText ?? task.partial;
  const restriction = remoteRestriction(task.error);
  return <>
    <div className="workbench-receipt-title"><StatusDot state={running ? 'processing' : task.state === 'waiting_approval' ? 'pending' : task.state}/>
      <span>{task.title}</span>
      {Number.isSafeInteger(progress?.done) && Number.isSafeInteger(progress?.total) && progress.total > 0 && progress.total <= 4
        && <ProgressDots {...progress} running={running} title="任务进度"/>}
      {completed && builtin && actions?.projectId && <SharedTaskOutcome projectId={actions.projectId} turnId={turn.id}
        documentId={task.document_id} historyScope={actions.scope}/>}
      {stopAction}
    </div>
    {builtin && Array.isArray(task.division) && task.division.length > 0
      && <TaskDivision project={actions?.projectId} turnId={turn.id} task={task} readOnly showEvidence={actions?.canRead === true}/>}
    {builtin && actions?.projectId && <SharedTaskRedoAction task={task} turn={turn} actions={actions} completed={completed}/>}
    {task.executor && task.executor !== 'builtin' && <ExternalTaskProgress receipt={task} readOnly/>}
    {streamFeedback}
    {partial && <CitedAnswer answer={partial}/>}
    {task.state === 'stopped' && <p role="status">已停止</p>}
    {task.error && <p role="status">
      {restriction?.reason || workbenchErrorMessage(task.error, '原处理未完成')}
      {restriction && actions?.openSettings && <button type="button" onClick={() => actions.openSettings(restriction.section)}>查看设置</button>}
    </p>}
  </>;
}

export function SharedHistoryReceipt({ turn, turnId, rememberActions, taskActions, streamText, streamFeedback, shareAction, stopAction }) {
  const receipt = turn.receipt, ask = receipt.ask, task = receipt.do, memory = receipt.remember;
  return <article id={`workbench-${turn.id}`} tabIndex={-1} className={turn.id === turnId ? 'is-target' : ''}>
    {turn.user_text && <div className="workbench-bubble">{displayScopedText(turn.user_text)}</div>}
    {turn.intent === 'multi' ? (receipt.parts || []).map(part => part.receipt?.remember?.items
      ? <GroupedRememberReceipt key={part.index} value={part.receipt.remember} renderRemember={(id, receipt) => <SharedHistoryReceipt turn={{ id, intent: 'remember', receipt: { remember: receipt } }}/>}/>
      : part.turn_id && part.receipt
      ? <SharedHistoryReceipt key={part.index} turn={{ id: part.turn_id, intent: part.intent, receipt: part.receipt }}/>
      : <Receipt key={part.index} kind={part.intent}><StatusDot state={part.state === 'failed' ? 'failed' : 'pending'}/></Receipt>)
      : <Receipt kind={turn.intent}>
        {ask ? <CitedAnswer answer={ask.answer ?? ask.partial ?? ''} citations={ask.citations || []} unverifiedSpans={ask.unverified_claim_spans}/>
          : task ? <SharedTaskReceipt task={task} turn={turn} actions={taskActions} streamText={streamText} streamFeedback={streamFeedback} stopAction={stopAction}/>
          : memory ? <SharedRememberReceipt memory={memory} turn={turn} actions={rememberActions}/>
          : receipt.inspiration?.insight ? <InsightChip insight={receipt.inspiration.insight}/> : null}
        {ask?.error && <p role="status">原处理未完成</p>}
        {ask?.interruption === 'stopped' && <p role="status">已停止</p>}
      </Receipt>}{shareAction}
  </article>;
}

export function useSharedHistoryScope(projectId, projectScope, scopeError, threadId, turnId) {
  const key = userStorageKey(`shared-workbench:${projectId}`);
  const binding = useMemo(() => ({ key, projectScope, scopeError, threadId, turnId }), [key, projectScope, scopeError, threadId, turnId]);
  const gate = useRequestScope(binding), scope = gate.scope;
  const [state, setState] = useState(null);
  const qualified = projectScope?.scope_kind === 'shared' && projectScope.id === projectId && !scopeError;
  return { key, gate, scope, qualified, setState, visible: qualified && state?.scope === scope ? state : null };
}

export function SharedWorkbenchHistory({ projectId, projectScope, scopeLoading, scopeError, onScopesRetry, threadId, turnId, onNavigate, draft, onDraft, RenderTurn, serverConversationShares }) {
  const { key, gate, scope, qualified, setState, visible } = useSharedHistoryScope(projectId, projectScope, scopeError, threadId, turnId);
  const actor = deviceIdentity();
  const stopGate = useRequestScope(`${key}:${actor.revision}:${threadId || ''}:${turnId || ''}`);
  const stopScope = stopGate.scope;
  const [retry, setRetry] = useState(0), [sharing, setSharing] = useState(null);
  const [stopping, setStopping] = useState(null), [stopError, setStopError] = useState(null);
  const activeStop = stopping?.scope === stopScope ? stopping : null;
  const observingStop = activeStop?.accepted ? activeStop : null;
  const stopRequest = useRef(null);
  useEffect(() => {
    if (!qualified) { setState(null); return () => gate.invalidate('history'); }
    const token = gate.issue('history', scope), actor = deviceIdentity();
    const controller = new AbortController();
    const current = () => !controller.signal.aborted && gate.isCurrent(token) && isDeviceIdentity(actor)
      && userStorageKey(`shared-workbench:${projectId}`) === key;
    let timer;
    async function read() {
      if (!current()) return;
      setState(state => state?.scope === scope && state.turns ? state : { scope, loading: true });
      try {
        let id = sharedHistoryThread(projectScope, observingStop?.thread || threadId, turnId, projectId), turns = [];
        const candidates = id ? [id] : turnId ? projectScope.threads.map(row => row.id) : [];
        for (const candidate of candidates) {
          const value = await workbenchApi.thread(projectId, candidate, { scopeKind: 'shared', signal: controller.signal });
          if (!current()) return;
          sharedHistoryDetail(value, candidate, projectId);
          if (!turnId || value.turns.some(turn => turn.id === turnId)) { id = candidate; turns = value.turns; break; }
        }
        if (turnId && !turns.some(turn => turn.id === turnId)) throw new Error('指定轮次不在本人的共享历史中');
        setState({ scope, id, turns });
        if (observingStop) {
          const observed = turns.find(turn => turn.id === observingStop.id);
          if (!observed) throw new Error('原回执未读取');
          setStopError(null);
          if (!canStopSharedReceipt(observed)) { setStopping(null); return; }
        }
      } catch (reason) {
        if (!current()) return;
        if (observingStop) setStopError({ scope: stopScope, message: reason.message });
        else setState({ scope, error: reason.message });
      }
      if (observingStop && current()) timer = setTimeout(read, 1500);
    }
    read();
    return () => { controller.abort(); clearTimeout(timer); gate.invalidate('history'); };
  }, [scope, stopScope, qualified, projectScope, retry, observingStop]);
  useEffect(() => {
    if (!turnId || !visible?.turns) return;
    const token = gate.issue('locate', scope), actor = deviceIdentity();
    const frame = requestAnimationFrame(() => {
      if (gate.isCurrent(token) && isDeviceIdentity(actor)) document.getElementById(`workbench-${turnId}`)?.scrollIntoView({ block: 'nearest' });
    });
    return () => { cancelAnimationFrame(frame); gate.invalidate('locate'); };
  }, [scope, visible?.turns, turnId]);
  async function stopTurn(turn) {
    if (!visible?.id || activeStop || stopRequest.current?.scope === stopScope || !canStopSharedReceipt(turn)) return;
    const token = stopGate.issue('stop', stopScope);
    if (!token) return;
    const pending = { scope: stopScope, thread: visible.id, id: turn.id };
    const current = () => stopGate.isCurrent(token) && isDeviceIdentity(actor);
    stopRequest.current = pending;
    setStopping(pending); setStopError(null);
    try {
      await workbenchApi.stop(projectId, turn.id);
      if (current()) setStopping({ ...pending, accepted: true });
    } catch (error) {
      if (current()) {
        setStopping(null);
        setStopError({ scope: stopScope, message: error.message });
        setRetry(value => value + 1);
      }
    } finally { if (stopRequest.current === pending) stopRequest.current = null; }
  }
  const actions = { readOnly: true, projects: [], stopTurn,
    stops: activeStop ? { [`${projectId}:${activeStop.id}`]: activeStop } : {}, openShare: serverConversationShares && qualified ? turn => {
    if (gate.isScopeCurrent(scope) && turn.scope_kind === 'shared')
      setSharing({ scope, source: { turnId: turn.id, projectId, scopeKind: 'shared' } });
  } : undefined, rememberActions: {
    projectId, canRead: qualified && projectScope?.membership === 'active' && projectScope.can_read_sources === true, onNavigate,
  }, taskActions: { projectId, scope, canRead: qualified && projectScope?.membership === 'active' && projectScope.can_read_sources === true,
    acceptRedo: result => setState(current => current?.scope !== scope || current.id !== result.thread_id ? current : {
      ...current, turns: current.turns.some(turn => turn.id === result.turn.id)
        ? current.turns.map(turn => turn.id === result.turn.id ? result.turn : turn) : [...current.turns, result.turn],
    }),
  } };
  return <div className="workbench-layout"><main className="workbench" aria-label="工作台">
    <p role="status">{projectScope?.membership === 'unavailable' ? '共享资格已失效 · 本人对话保留' : projectScope?.role === 'viewer' ? '共享 · 只看' : '共享'} · 当前不可发送</p>
    <button type="button" onClick={onScopesRetry}>刷新资格</button>
    {(scopeLoading || visible?.loading) && <StatusDot state="processing"/>}
    {stopError?.scope === stopScope && <p role="alert">{stopError.message}</p>}
    {(scopeError || visible?.error) && <p role="alert">{scopeError || visible.error} · <button type="button" onClick={() => { onScopesRetry?.(); setRetry(value => value + 1); }}>重新读取</button></p>}
    {!!projectScope?.threads.length && <div className="workbench-history"><select aria-label="对话" value={visible?.id || ''} onChange={event => onNavigate?.('workbench', { scope_kind: 'shared', project_id: projectId, thread_id: event.target.value })}>
      {!visible?.id && <option value="">选择本人对话</option>}{projectScope.threads.map(thread => <option key={thread.id} value={thread.id}>{displayScopedText(thread.title)}</option>)}
    </select></div>}
    <div className="workbench-thread">{visible?.turns?.map(turn => <RenderTurn key={turn.id} turn={turn} turnId={turnId} projectId={projectId} scope={scope} actions={actions}/>)}</div>
    {!scopeLoading && !scopeError && !projectScope && <p role="status">当前共享项目范围不可用</p>}
    <div className="workbench-composer"><Composer draft={draft} onDraft={onDraft} projectTag={projectScope?.name || projectId} fixedIntent="ask" allowFiles={false} sendDisabled/></div>
  </main>{serverConversationShares && sharing?.scope === scope && <RemoteConversationShare source={sharing.source}
    onClose={() => setSharing(null)} onNavigate={onNavigate}/>}</div>;
}
