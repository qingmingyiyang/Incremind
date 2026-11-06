import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Composer, Receipt, InsightChip, ProgressDots, StatusDot, CitedAnswer, LayerBadges, FocusPanel, ConversationSharePreview, MarkdownBody, Icon } from '../../shared/ui';
import { readStoredJson, writeStoredJson, removeStoredItem } from '../../shared/lib/browserStorage';
import { OutcomeFreshness } from '../../shared/ui/OutcomeFreshness';
import { userStorageKey, deviceIdentity, isDeviceIdentity, readDeviceCredential } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { answerWithoutCitationMarks } from '../../shared/ui/CitedAnswer';
import { createConversationSnapshot } from '../../shared/lib/conversationSnapshot';
import { RemoteConversationShare } from '../conversation-shares/RemoteConversationShare';
import { displayScopedText, formatScopeTag, scopedText } from '../../shared/lib/scopeTag';
import { sendSignal, signalEpoch } from '../../shared/signalsApi';
import { recognitionApi } from '../../shared/api/recognitionApi';
import { remoteRestriction } from '../../shared/lib/privacyMessages';
import { SharedWorkbenchHistory, SharedHistoryReceipt, canStopSharedReceipt } from '../shared-projects/SharedWorkbenchHistory';
import { sharedAskRequest, sharedInspirationRequest, sharedRememberRequest, sharedTaskRequest, sharedTaskRedoRevision, sharedTaskTextTooLong } from './workbenchScope';
import { sharedRememberResult, useSharedRemember } from '../shared-projects/useSharedRemember';
import { sharedTaskAcceptance, sharedTaskCommandResult, sharedTaskNotReceived, useSharedTaskSubmission } from '../shared-projects/useSharedTaskSubmission';
import { useSharedInspirations } from '../shared-projects/useSharedInspirations';
import { SharedInspirationReceipts } from '../shared-projects/SharedInspirationReceipts';
import { workbenchApi, workbenchErrorMessage } from './workbenchApi';
import { libraryApi } from '../library/libraryApi';
import { ContextPanel } from './ContextPanel';
import { CitationBody } from './CitationBody';
import { WorkbenchDraftPanel } from './WorkbenchDraftPanel';
import { WorkbenchOriginalPanel } from './WorkbenchOriginalPanel';
import { WorkbenchOutcomePanel } from './WorkbenchOutcomePanel';
import { TaskDivision } from './TaskDivision';
import { ExternalTaskProgress } from './ExternalTaskProgress';
import { Interventions } from './Interventions';
import { updatePartAnswer } from './interventionState';
import { readOutcomeUpdate } from './outcomeSources';
import { LexiconChangeReceipt } from './LexiconChangeReceipt';
import { RouteCorrectionPanel } from './RouteCorrectionPanel';
import { GroupedRememberReceipt } from './GroupedRememberReceipt';
import { WorkbenchHome } from './WorkbenchHome';
import { ExternalExecutorButton, ExternalExecutorPicker, useExternalExecutor, EXECUTOR_LABELS } from './ExternalExecutorPicker';
import { DocumentPlacement } from '../library/DocumentPlacement';
import { InboxFiling } from '../library/InboxFiling';
import { OutcomeVersion } from './OutcomeChanges';
import { Row } from '../../shared/ui';
import './workbench.css';

const EMPTY_PROJECTS = [];
const retryReasons = { server: '服务商过载', connection: '网络连接', timeout: '响应超时',
  stalled: '响应停顿', rate_limit: '限流', header_timeout: '响应超时', malformed_stream: '响应格式' };
function RetryCountdown({ status }) {
  const retry = status?.retry;
  const valid = status?.state === 'retrying' && retryReasons[retry?.reason]
    && Number.isSafeInteger(retry.used) && Number.isSafeInteger(retry.limit)
    && retry.used > 0 && retry.used <= retry.limit
    && Number.isFinite(retry.delay) && retry.delay >= 0
    && Number.isFinite(retry.retry_at) && Number.isFinite(status.server_time);
  const duration = valid ? Math.max(0, Math.min(retry.delay, retry.retry_at - status.server_time)) * 1000 : 0;
  const [seconds, setSeconds] = useState(Math.ceil(duration / 1000));
  useEffect(() => {
    const deadline = performance.now() + duration;
    const tick = () => setSeconds(Math.ceil(Math.max(0, deadline - performance.now()) / 1000));
    tick();
    if (!valid) return;
    const timer = setInterval(tick, 1000);
    return () => clearInterval(timer);
  }, [status, duration, valid]);
  return valid ? <span className="workbench-retry-status" title={retryReasons[retry.reason]}>重试 {retry.used}/{retry.limit} · {seconds}s</span> : null;
}

function contextPercent(context) {
 const parts=context?.parts, window=context?.window;
 if (!Array.isArray(parts) || !parts.length || !Number.isFinite(window) || window<=0
   || parts.some(part=>!Number.isFinite(part.tokens) || part.tokens<0)) return '—';
 return `${Math.round(parts.reduce((sum,part)=>sum+part.tokens,0)/window*1000)/10}%`;
}
function targetProject(text, intent, projectId, projects) {
  const tag = scopedText(text).project;
  if (!tag) return intent === 'inspiration' || (!intent && /^\s*灵感/.test(text)) ? 'inbox' : projectId;
  const choices = new Map(projects.map(project => [project.id, project.name]));
  for (const [id, name] of [['default', '日常'], ['inbox', '收件箱'], ['me', '我']]) if (!choices.has(id)) choices.set(id, name);
  // 旧写法 #默认 仍指日常，除非真有项目叫这个名字。
  if (tag === '默认' && !projects.some(project => project.name === '默认')) return 'default';
  if (choices.has(tag)) return tag;
  const ids = [...choices].filter(([, name]) => name === tag).map(([id]) => id);
  if (ids.length !== 1) {
    const failure = new Error(ids.length ? '项目重名 · 使用项目编号' : '项目未找到 · 修改标签');
    if (!ids.length) failure.projectTag = tag;
    throw failure;
  }
  return ids[0];
}
const REDO_LABELS = { remember: '换成记住', ask: '换成问', do: '换成干活' };
// 多部分回合里，任一部分还没结束就继续轮询。
const busyReceipt = receipt => receipt.remember?.state === 'processing'
  || ['researching', 'preparing', 'running'].includes(receipt.do?.state)
  || (receipt.parts || []).some(part => ['waiting', 'running'].includes(part.state) || busyReceipt(part.receipt || {}));
function receiptTurn(turns, id) {
  for (const turn of turns) {
    if (turn.id === id) return turn;
    const child = receiptTurn((turn.receipt?.parts || []).filter(part => part.turn_id).map(part => ({
      id: part.turn_id, intent: part.intent, receipt: part.receipt || {},
    })), id);
    if (child) return child;
  }
  return null;
}
function replaceReceiptTurn(turns, updated) {
  return turns.map(turn => turn.id === updated.id ? updated : { ...turn, receipt: { ...turn.receipt,
    ...(turn.receipt.parts && { parts: turn.receipt.parts.map(part => part.turn_id === updated.id
      ? { ...part, receipt: updated.receipt, state: updated.receipt.remember?.state || updated.receipt.do?.state || 'done' }
      : { ...part, receipt: replaceReceiptTurn([{ id: part.turn_id, receipt: part.receipt || {} }], updated)[0].receipt }) }),
  } });
}
const stoppedTurn = turn => !turn?.receipt?.ask?.error && turn?.receipt?.ask?.interruption === 'stopped'
  || turn?.receipt?.do?.state === 'stopped';
const settledStop = turn => stoppedTurn(turn) || ['done', 'partial', 'failed', 'cancelled'].includes(turn?.receipt?.do?.state)
  || Boolean(turn?.receipt?.ask?.error)
  || typeof turn?.receipt?.ask?.answer === 'string' && !turn.receipt.ask.interruption;
function StopButton({ disabled, onClick }) {
  return <button type="button" className="workbench-stop" aria-label="停止" title="停止" disabled={disabled} onClick={onClick}>
    <Icon name="review-stop" size={14}/>
  </button>;
}
function WorkbenchError({ error, fallback, projectId, onSettings, onRetry, retryDisabled = false, readFailure = false }) {
  if (!error) return null;
  const restriction = remoteRestriction(error);
  const message = readFailure ? '读取未完成' : fallback && typeof error === 'string'
    && !['interrupted', 'storage_quota_exceeded', 'job_quota_exceeded'].includes(error)
    ? fallback : workbenchErrorMessage(error, fallback);
  return <div role="alert" className="workbench-error" title={restriction?.title}>
    {restriction ? <><span>{restriction.reason}</span><span aria-hidden="true"> · </span><button type="button" disabled={!onSettings}
      onClick={() => onSettings?.(restriction.section, projectId)}>查看设置</button></> : message}
    {onRetry && <><span aria-hidden="true"> · </span><button type="button" disabled={retryDisabled} onClick={onRetry}>重试</button></>}
  </div>;
}
function TaskRetryAction({ task, turn, projectId, scope, disabled, onRetry, shared = false }) {
  const receipt = useRef({task, scope}), generation = useRef(0);
  if (receipt.current.task !== task || receipt.current.scope !== scope) { receipt.current = {task, scope}; generation.current++; }
  const gate = useRequestScope(`${userStorageKey('task-retry')}:${projectId}:${turn.id}:${generation.current}`);
  const [checked, setChecked] = useState(null), [busy, setBusy] = useState(false), [error, setError] = useState(null);
  const ready = task.retryable === true || checked?.scope === gate.scope;
  useEffect(() => { setBusy(false); setError(null); setChecked(null); }, [gate.scope]);
  async function check() {
    const token = gate.issue('check', gate.scope);
    if (!token) return;
    setBusy(true); setError(null);
    try {
      const source = await workbenchApi.division(projectId, turn.id, {for_redo:true});
      if (shared) sharedTaskRedoRevision(source);
      if (gate.isCurrent(token)) setChecked({scope:gate.scope});
    } catch (failure) { if (gate.isCurrent(token)) setError(failure); }
    finally { if (gate.isCurrent(token)) setBusy(false); }
  }
  if (!ready && !task.retry_check_required) return null;
  return <><button type="button" disabled={disabled || busy} onClick={ready ? onRetry : check}>{ready ? '重试' : busy ? '检查中' : '检查重试'}</button>
    <WorkbenchError error={error} projectId={projectId}/></>;
}
const WorkbenchTurn = memo(function WorkbenchTurn({ turn, turnId, projectId, errorProjectId = projectId, shareQuestion = turn.user_text, displayOwner, scope, reviewing, continuing, streamText, streamStatus, streamProgress, actions }) {
  const { review, retry, continueTurn, openOutput, openCitation, openShare, redoOutput, chooseOutput, closeOutput, closeCitation,
    setTracePanel, setDraftPanel, setSession, onNavigate, openSettings, askElsewhere, projects, sending, refreshPlacement, setError, stopTurn, stops,
    saveAnswerOutcome, savingOutcome, openCorrection } = actions;
  const [copied, setCopied] = useState(false), copyTimer = useRef(null), currentScope = useRef(scope), alive = useRef(true);
  const [more, setMore] = useState(false), moreButton = useRef(null);
  currentScope.current = scope;
  useEffect(() => { alive.current = true; setCopied(false); return () => { alive.current = false; clearTimeout(copyTimer.current); }; }, [scope]);
  if (actions.readOnly) {
    const ask = turn.receipt.ask, task = turn.receipt.do;
    const shareable = turn.scope_kind === 'shared' && (typeof ask?.answer === 'string' && !ask.error && !ask.interruption
      || ['done', 'partial'].includes(task?.state) && task.document_id && !task.executor);
    const shareAction = actions.openShare && shareable && <div className="ui-conversation-menu"
      onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setMore(false); }}
      onKeyDown={event => { if (event.key === 'Escape') { event.stopPropagation(); setMore(false); moreButton.current?.focus(); } }}>
      <button ref={moreButton} type="button" aria-label="更多" aria-haspopup="menu" aria-expanded={more}
        onClick={() => setMore(value => !value)}><Icon name="more" size={16}/></button>
      {more && <div role="menu" aria-label="对话动作"><button type="button" role="menuitem" onClick={() => {
        setMore(false); moreButton.current?.focus(); actions.openShare(turn, projectId);
      }}>分享</button></div>}
    </div>;
    const stopAction = stopTurn && canStopSharedReceipt(turn)
      ? <StopButton disabled={Boolean(stops[`${projectId}:${turn.id}`])} onClick={() => stopTurn(turn, projectId)}/> : null;
    return <SharedHistoryReceipt turn={turn} turnId={turnId} rememberActions={actions.rememberActions} shareAction={shareAction} stopAction={stopAction}
      taskActions={actions.taskActions} streamText={streamText} streamFeedback={streamStatus && <RetryCountdown status={streamStatus}/>}/>;
  }
  const recordCopy = (epoch = signalEpoch()) => sendSignal({ kind: 'copy', project_id: projectId, turn_id: turn.id }, epoch);
  async function copyAnswer() {
    const epoch = signalEpoch(), capturedScope = scope;
    try { await navigator.clipboard.writeText(answerWithoutCitationMarks(turn.receipt.ask.answer ?? turn.receipt.ask.partial ?? '', turn.receipt.ask.citations)); } catch { return; }
    if (!alive.current || currentScope.current !== capturedScope) return;
    recordCopy(epoch); setCopied(true); clearTimeout(copyTimer.current);
    copyTimer.current = setTimeout(() => setCopied(false), 1500);
  }
  // An automatically filed item's insights live in its new project.
  const chip = (insight, project) => <span key={insight.id} className="workbench-insight-open"><InsightChip insight={insight} onConfirm={reviewing ? undefined : value => review(value, 'confirm', project)} onDrop={reviewing ? undefined : value => review(value, 'drop', project)}/>{insight.state === 'pending' && <button type="button" aria-label={`打开认识 ${insight.text}`} onClick={() => openCitation({ layer: 'insight', id: insight.id, project })}><Icon name="arrow-up-right" size={14}/></button>}</span>;
      const redoItems = turn.user_text && !['inspiration', 'multi'].includes(turn.intent) ? ['remember', 'ask', 'do'].filter(key => key !== turn.intent)
        .map(key => <button key={key} type="button" role="menuitem" disabled={sending} onClick={() => { setMore(false); moreButton.current?.focus(); openCorrection(turn, projectId, key); }}>{REDO_LABELS[key]}</button>) : [];
      const moreMenu = items => <div className="ui-conversation-menu" onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setMore(false); }} onKeyDown={event => {
        if (event.key === 'Escape') { event.stopPropagation(); setMore(false); moreButton.current?.focus(); }
      }}><button ref={moreButton} type="button" aria-label="更多" title="更多" aria-haspopup="menu" aria-expanded={more} onClick={() => setMore(value => !value)}><Icon name="more" size={16}/></button>
        {more && <div role="menu" aria-label="对话动作">{items}{turn.intent !== 'multi' && correctionAction}</div>}
      </div>;
      const correctionAction = typeof turn.user_text === 'string' && !turn.receipt.reminder && !busyReceipt(turn.receipt)
        && (turn.intent !== 'multi' || turn.receipt.parts?.length > 0)
        ? <button type="button" role="menuitem" disabled={sending} onClick={() => { setMore(false); moreButton.current?.focus(); openCorrection(turn, projectId); }}>纠正分段</button> : null;
      const replacement = typeof turn.user_text === 'string' && turn.route_correction
        ? <span className="ui-count" aria-label="原结果已替换">已替换</span> : null;
      if (turn.intent === 'multi') {
        const parts = turn.receipt.parts || [];
        return <article key={turn.id} id={`workbench-${turn.id}`} tabIndex={-1} className={turn.id === turnId ? 'is-target' : ''}>{turn.user_text && <div className="workbench-bubble">{displayScopedText(turn.user_text)}</div>}
          <div className="workbench-parts" aria-label="拆分">{parts.map(part => <span key={part.index} title={part.span}><Icon name={part.intent} size={12}/>{part.span.length > 12 ? part.span.slice(0, 12) + '…' : part.span}</span>)}{!parts.length && <span role="status">已移除全部部分</span>}{replacement}{(correctionAction || !!redoItems.length) && moreMenu(<>{correctionAction}{redoItems}</>)}</div>
          {parts.map(part => part.receipt?.remember?.items
            ? <GroupedRememberReceipt key={part.index} value={part.receipt.remember} renderRemember={(id, receipt, item) => <WorkbenchTurn turn={{ ...turn, id, intent: 'remember', user_text: null, receipt: { remember: receipt } }} displayOwner={turn.id} shareQuestion={part.span} turnId={turnId} projectId={item.project_id || part.project_id || projectId} scope={scope} reviewing={reviewing} continuing={continuing} actions={actions}/>}/>
            : part.turn_id && part.receipt && Object.keys(part.receipt).length
            ? <WorkbenchTurn key={part.index} turn={{ ...turn, id: part.turn_id, intent: part.intent, user_text: null, receipt: part.receipt }} displayOwner={part.source_members?.some(member => member.turn_id === part.turn_id) ? turn.id : undefined} shareQuestion={part.span} turnId={turnId} projectId={part.project_id || projectId} errorProjectId={part.project_id || errorProjectId} scope={scope} reviewing={reviewing} continuing={continuing} actions={actions}/>
            : <Receipt key={part.index} kind={part.intent}><StatusDot state={['failed', 'not_started', 'interrupted'].includes(part.state) ? 'failed' : part.state === 'running' ? 'processing' : 'pending'}/>{part.error && <WorkbenchError error={part.error} projectId={part.project_id || errorProjectId} onSettings={openSettings}/>}</Receipt>)}
        </article>;
      }
      const memory = turn.receipt.remember, inspiration = turn.receipt.inspiration, ask = turn.receipt.ask;
      const savedTask = turn.receipt.do;
      const task = savedTask && streamProgress && savedTask.executor === streamProgress.usage.source
        ? { ...savedTask, external_progress: streamProgress, external_usage: streamProgress.usage } : savedTask;
      const externalTask = Boolean(task?.executor && task.executor !== 'builtin');
      const externalTaskError = typeof task?.error === 'string' ? task.error : task?.error?.code;
      const interruption = (ask || task)?.interruption;
      const interrupted = !ask?.error && ['connection', 'sleep', 'stopped'].includes(interruption);
      const stopping = Boolean(stops[`${projectId}:${turn.id}`]);
      const canContinue = Boolean(ask) && !ask.error || !externalTask && ['interrupted', 'stopped'].includes(task?.state);
      const stopControl = (task && ['researching', 'preparing', 'running', 'waiting_approval'].includes(task.state)
        || ask && !ask.error && !ask.interruption && ask.answer === null)
        ? <StopButton disabled={stopping} onClick={() => stopTurn(turn, projectId)}/> : null;
      const hint = ask?.elsewhere, hintName = hint && (projects.find(project => project.id === hint.project_id)?.name || hint.project_id);
      const hintScene = hint?.scene;
      const hintLabel = hint && `#${hintName}${hintScene ? '/' + hintScene : ''}`;
      const shareable = typeof ask?.answer === 'string' || typeof inspiration?.insight?.text === 'string'
        || Boolean(memory?.state === 'done' && memory.document_id)
        || Boolean(['done', 'partial'].includes(task?.state) && task.document_id);
      const canSaveOutcome = turn.intent === 'ask' && typeof ask?.answer === 'string' && Boolean(ask.answer.trim())
        && !ask.error && !ask.interruption;
      const savingThisOutcome = savingOutcome?.project === projectId && savingOutcome.turnId === turn.id;
      const shareMenu = moreMenu(<>{memory?.item_id && !memory.document_id && <button type="button" role="menuitem" aria-label="打开原件" onClick={() => { setMore(false); moreButton.current?.focus(); closeOutput(); closeCitation(); setTracePanel(null); setDraftPanel({ scope, projectId, itemId: memory.item_id }); }}><Icon name="arrow-up-right" size={14}/>原件</button>}<button type="button" role="menuitem" disabled={!shareable} title={shareable ? undefined : '完成后分享'} onClick={() => { setMore(false); moreButton.current?.focus(); openShare({ ...turn, user_text: shareQuestion }, projectId); }}>分享</button>{canSaveOutcome && <button type="button" role="menuitem" disabled={sending || continuing || Boolean(savingOutcome)} onClick={() => { setMore(false); moreButton.current?.focus(); saveAnswerOutcome(turn, projectId); }}>{savingThisOutcome ? '保存中' : '存为成果'}</button>}{redoItems}</>);
      return <article key={turn.id} id={`workbench-${displayOwner ? displayOwner + '-' : ''}${turn.id}`} tabIndex={-1} className={turn.id === turnId ? 'is-target' : ''}>
        {turn.user_text && <div className="workbench-bubble">{displayScopedText(turn.user_text)}</div>}
        <Receipt kind={turn.intent} reminder={turn.receipt.reminder} onNavigate={onNavigate}>
        {task && !externalTask && <LexiconChangeReceipt projectId={projectId} turnId={turn.id} task={task}
          onUpdated={updated => setSession(current => current.project === projectId
            ? { ...current, turns: replaceReceiptTurn(current.turns, updated) } : current)}/>}
        {memory ? <>
          <div className="workbench-receipt-title"><StatusDot state={memory.state}/><span className="workbench-memory-title" title={memory.title}>{memory.title}</span><ProgressDots {...memory.progress} running={memory.state === 'processing'} title="原件 · 正文 · 整理 · 入库"/>{memory.document_id && <button type="button" aria-label="打开整理稿" onClick={() => { closeOutput(); closeCitation(); setTracePanel(null); setDraftPanel({ scope, documentId: memory.document_id, itemId: memory.item_id }); }}>↗</button>}{memory.state === 'failed' && <><button type="button" disabled={sending || continuing} onClick={() => retry(turn, projectId)}>重试</button>{memory.item_id && <button type="button" aria-label="打开原件" onClick={() => { closeOutput(); closeCitation(); setTracePanel(null); setDraftPanel({ scope, projectId, itemId: memory.item_id }); }}>↗</button>}</>}</div>
          <DocumentPlacement projectId={projectId} projects={projects} row={{...memory,revision:memory.document_revision}}
            allowShared={actions.allowSharedFiling} onNavigate={onNavigate} onChanged={refreshPlacement} onError={setError}/><div className="workbench-insights">{memory.insights.map(insight => chip(insight, memory.filing?.state === 'filed' && memory.filing.target_scope_kind !== 'shared' ? memory.filing.target_project_id : undefined))}{!!memory.related.length && <span className="workbench-related-count" title={memory.related.map(entry => entry.text).join('\n')}>∞ {memory.related.length}</span>}</div><WorkbenchError error={memory.error} fallback="整理未完成 · 重试" projectId={errorProjectId} onSettings={openSettings}/>
        </> : ask ? <>
          {stopControl && <div className="workbench-receipt-title">{stopControl}</div>}
          <CitedAnswer answer={ask.answer ?? ask.partial ?? ''} citations={ask.citations} unverifiedSpans={ask.unverified_claim_spans} onOpenCitation={openCitation} citationHref="#workbench-citation" onCopyAnswer={() => recordCopy()}/><div className="workbench-ask-layers"><LayerBadges layers={ask.layers} onOpenTrace={() => { closeOutput(); closeCitation(); setTracePanel({ scope, projectId, ask }); }}/>{hint && <span className="ui-layer-badges workbench-elsewhere"><button type="button" title={`在 ${hintLabel} 里问`} aria-label={`在 ${hintLabel} 里问`} disabled={sending} onClick={() => askElsewhere(turn, hint)}>→ {hintLabel}</button></span>}<button type="button" className="workbench-answer-copy" aria-label="复制回答" title="复制" onClick={copyAnswer}><Icon name={copied ? 'check' : 'copy'} size={14}/></button></div>
        </> : task ? <>
          <div className="workbench-receipt-title"><StatusDot state={['researching', 'preparing', 'running'].includes(task.state) ? 'processing' : task.state === 'waiting_approval' ? 'pending' : task.state}/><span className="workbench-memory-title" title={task.title}>{task.title}</span><button type="button" className="workbench-context-open" aria-label="本次上下文" onClick={() => { closeOutput(); closeCitation(); setTracePanel({ scope, projectId, ask: task, kind: 'do' }); }}>◯ {contextPercent(task.context)}</button>{!externalTask && <ProgressDots {...task.progress} running={['researching', 'preparing', 'running'].includes(task.state)} title={task.kernel_turn_id ? '拆活 · 干活 · 汇总' : task.research_turn_id ? '研究 · 准备 · 批准 · 成果' : '准备 · 批准 · 成果'}/>}<OutcomeVersion version={task.continues?.version}/>{task.executor && task.executor !== 'builtin' && EXECUTOR_LABELS[task.executor] && <span className="workbench-executor-source">来自 {EXECUTOR_LABELS[task.executor]}</span>}{task.fallback_new && <span className="ui-count" aria-label="已新写一篇">新写一篇</span>}{!externalTask && ['done','partial'].includes(task.state) && task.document_id && <OutcomeReceiptChoices turn={turn} onNew={() => redoOutput(turn, null)} onOther={() => chooseOutput(turn)}/>}{['done','partial'].includes(task.state) && task.document_id && <button type="button" aria-label="打开成果" onClick={() => openOutput(task)}>↗</button>}{stopControl}</div>
          {!externalTask && task.kernel_turn_id && <TaskDivision key={`${projectId}:${turn.id}`} project={projectId} turnId={turn.id} task={task} onReference={reference => onNavigate?.('workbench', reference)} onRedo={result => setSession(current => current.project === projectId ? { ...current, id: result.thread_id, turns: current.turns.some(row => row.id === result.turn.id) ? current.turns : [...current.turns, result.turn] } : current)}/>}{!task.kernel_turn_id && !!task.experts?.length && <div className="workbench-experts">{task.experts.map((expert, index) => <span key={index} role="img" aria-label={expert.role} title={expert.role}><StatusDot aria-hidden="true" state={expert.state === 'running' ? 'processing' : expert.state}/></span>)}</div>}
          {externalTask && <ExternalTaskProgress receipt={task}/>}
          <OutcomeFreshness stale={task.stale} freshCount={task.fresh_count} verified={task.changes_verified}/>
        </> : inspiration?.insight ? <>{chip(inspiration.insight, projectId)}{inspiration.placement && projects.some(project=>project.id===inspiration.placement.project_id) && <InboxFiling key={`${scope}:${inspiration.insight.id}:${inspiration.insight.revision}`} row={inspiration.insight} targetProjectId={inspiration.placement.project_id} scenes={projects.find(project=>project.id===inspiration.placement.project_id)?.scenes || []} suggestion={inspiration.placement.scene} onFiled={refreshPlacement} onError={setError}/>}</> : null}
          {!memory && (ask?.error || task?.error) && <WorkbenchError error={externalTask ? externalTaskError || 'external_task_execution_failed' : (ask || task).error} fallback={externalTask ? task.state === 'interrupted' || externalTaskError === 'external_task_interrupted' ? '外部执行已中断' : '外部执行未完成' : undefined} projectId={errorProjectId} onSettings={openSettings}/>}
          {!externalTask && task?.state === 'failed' && <TaskRetryAction task={task} turn={turn} projectId={projectId} scope={scope} disabled={sending || actions.retrying} onRetry={() => redoOutput(turn)}/>}
          {task && interrupted && <>{interruption !== 'stopped' && <RetryCountdown status={streamStatus}/>}<CitedAnswer answer={interruption === 'stopped' ? task.partial || '' : streamText ?? task.partial ?? ''}/></>}
          {interrupted && <div className="workbench-interrupted"><span>{interruption === 'stopped' ? '已停止' : interruption === 'sleep' ? '电脑休眠' : '连接中断'}</span>{canContinue && <><span aria-hidden="true">·</span><button type="button" disabled={continuing || stopping} onClick={() => continueTurn(turn, projectId)}>继续</button></>}</div>}
        </Receipt>{replacement}{shareMenu}
      </article>;
});

export function Workbench(props) {
  const [acceptedShared, setAcceptedShared] = useState(null);
  const retainShared = props.projectScopeKind === 'shared' && acceptedShared?.projectId === props.projectId
    && isDeviceIdentity(acceptedShared.actor) && (!props.threadId || props.threadId === acceptedShared.threadId)
    && (!props.turnId || props.turnId === acceptedShared.turnId);
  if (props.projectScopeKind && props.projectScopeKind !== 'personal'
    && (props.projectScopeKind !== 'shared' || props.scopeError || props.projectScope?.id !== props.projectId
      || props.projectScope?.membership !== 'active' || !(props.projectScope.can_ask || props.projectScope.can_write || props.projectScope.can_do === true))
    && !retainShared)
    return <SharedWorkbenchHistory {...props} RenderTurn={WorkbenchTurn}/>;
  return <PersonalWorkbench {...props} onSharedAccepted={setAcceptedShared}/>;
}

function useWorkbenchScope(key) {
  const actor = deviceIdentity(), original = useRequestScope(`${userStorageKey('workbench')}:${actor.revision}:${key}`);
  return useMemo(() => ({
    get scope() { return original.scope; },
    issue: (...args) => isDeviceIdentity(actor) ? original.issue(...args) : null,
    isCurrent: token => isDeviceIdentity(actor) && original.isCurrent(token),
    isScopeCurrent: value => isDeviceIdentity(actor) && original.isScopeCurrent(value),
    invalidate: channel => original.invalidate(channel),
  }), [original, actor.key, actor.revision]);
}

function PersonalWorkbench({ projectId = 'default', projectScopeKind: scopeKind = 'personal', projectScope: currentProjectScope, scopeError, projects = EMPTY_PROJECTS, threadId, turnId, onNavigate, draft, onDraft, handoff, onHandoff, onProjectCreated, onProjectsStale, inputProposal, onInputProposalHandled, serverConversationShares, onSharedAccepted }) {
  const isShared = scopeKind === 'shared';
  const projectScope = !scopeError && currentProjectScope?.id === projectId && currentProjectScope.membership === 'active'
    ? currentProjectScope : null;
  const allowExternalExecutor = !isShared && !readDeviceCredential()?.mobile;
  const externalExecutor = useExternalExecutor(projectId, { enabled: allowExternalExecutor });
  const gate = useWorkbenchScope(`${scopeKind}:${projectId}:${threadId || ''}:${turnId || ''}`), scope = gate.scope;
  const [session, setSession] = useState({ scope: threadId || turnId ? null : scope, project: projectId, threads: [], id: null, turns: [] });
  const sessionReady = session.scope === scope && session.project === projectId;
  const visible = sessionReady ? session : { threads: [], id: null, turns: [] };
  const selectedThread = useRef(visible.id);
  selectedThread.current = visible.id;
  const [threadReadFailure, setThreadReadFailure] = useState(null), [threadReadRetry, setThreadReadRetry] = useState(0);
  const [errorState, setErrorState] = useState(null), [busy, setBusy] = useState(false), [reviewing, setReviewing] = useState(null);
  const error = errorState?.scope === scope ? errorState : null;
  const setError = useCallback((failure, targetProject = projectId) => {
    if (!gate.isScopeCurrent(scope)) return;
    setThreadReadFailure(null);
    // 保留原错误码和请求项目，普通文字提示仍可沿用这个入口。
    const value = typeof failure === 'string' ? { message: failure } : failure;
    setErrorState(failure ? { scope, projectId: targetProject, code: value?.code,
      message: workbenchErrorMessage(value) } : null);
  }, [gate, scope, projectId]);
  const openSettings = useCallback((section, targetProject = projectId) => {
    if (gate.isScopeCurrent(scope) && ['privacy', 'model', 'project'].includes(section))
      onNavigate?.('settings', { section, project_id: isShared ? projects.find(row => row.id === 'default')?.id || projects[0]?.id || 'default' : targetProject, scope_kind: 'personal' });
  }, [gate, scope, projectId, onNavigate, isShared, projects]);
  const [retrying, setRetrying] = useState(false);
  const sharedRetry = useRef(null);
  const hasSharedRetry = useCallback(() => sharedRetry.current?.scope === scope, [scope]);
  const [recording, setRecording] = useState(false);
  const [tracePanel, setTracePanel] = useState(null);
  const [citationPanel, setCitationPanel] = useState(null);
  const [outputPanel, setOutputPanel] = useState(null);
  const [continuation, setContinuation] = useState(null), [choicePanel, setChoicePanel] = useState(null);
  const [example, setExample] = useState(null);
  const continuationSequence = useRef(0);
  const outcomeRedoPending = useRef(null);
  const [draftPanel, setDraftPanel] = useState(null);
  const [sharePanel, setSharePanel] = useState(null);
  const [correctionPanel, setCorrectionPanel] = useState(null);
  const [saveOutcomeState, setSaveOutcomeState] = useState(null), outcomeSavePending = useRef(null);
  const savingOutcome = saveOutcomeState?.scope === scope && isDeviceIdentity(saveOutcomeState.identity) ? saveOutcomeState : null;
  const [streaming, setStreaming] = useState(null);
  const [interventionDraft, setInterventionDraft] = useState(null);
  const preparingOutcome = useRef(null);
  const [stopState, setStopState] = useState({ scope, items: {} });
  const stops = useMemo(() => stopState.scope === scope ? stopState.items : {}, [stopState, scope]);
  const stopRequests = useRef(new Map());
  const clearFinishedStop = useCallback(failure => {
    const finished = failure.finishedTurn;
    if (!finished || !gate.isScopeCurrent(scope)) return;
    const key = `${finished.project}:${finished.id}`;
    // 业务终态可能没有生成回执，只释放这次真实请求对应的停止观察。
    setStopState(current => current.scope === scope && current.items[key] ? { scope,
      items: Object.fromEntries(Object.entries(current.items).filter(([name]) => name !== key)) } : current);
  }, [gate, scope]);
  const [continueState, setContinueState] = useState(null);
  const pendingContinue = continueState?.scope === scope ? continueState : null;
  const [unknownProject, setUnknownProject] = useState(null), [composerRevision, setComposerRevision] = useState(0);
  const [continuationReset, setContinuationReset] = useState(0);
  const creatingProject = useRef(false);
  const streamController = useRef(null);
  const attachmentRetry = useRef(null);
  const attachmentUploads = useRef([]);
  const resumedTurns = useRef({ scope: null, ids: new Set() });
  const locatedTurn = useRef(null);
  const recorderRef = useRef(null), recordingResolveRef = useRef(null), startingRecord = useRef(false);
  const cacheKey = userStorageKey(`chriptmas-v2-thread:${isShared ? 'shared:' : ''}${projectId}`);
  const sharedInspirations = useSharedInspirations({ enabled: isShared, canWrite: projectScope?.can_write === true, projectId });
  const sharedRemember = useSharedRemember({ enabled: isShared, canWrite: projectScope?.can_write === true, projectId });
  const sharedTask = useSharedTaskSubmission({ enabled: isShared, projectId });
  const rememberWorking = sharedRemember.isWorking, inspirationWorking = sharedInspirations.isWorking;
  const sharedOperationPending = useCallback(() => Boolean(hasSharedRetry() || rememberWorking() || inspirationWorking()
    || outcomeRedoPending.current?.scope === scope || streamController.current && !streamController.current.signal.aborted),
  [hasSharedRetry, rememberWorking, inspirationWorking, scope]);
  function retryThreadRead() {
    if (gate.isScopeCurrent(scope) && threadReadFailure === scope && !busy && !reviewing && !retrying)
      setThreadReadRetry(value => value + 1);
  }
  useEffect(() => () => { streamController.current?.abort(); }, [scope]);
  useEffect(() => {
    const clear = () => {
      outcomeSavePending.current?.controller.abort(); outcomeSavePending.current = null;
      gate.invalidate('save-outcome'); setSaveOutcomeState(null);
    };
    window.addEventListener('chriptmas-user-space', clear);
    window.addEventListener('chriptmas-device-required', clear);
    return () => {
      window.removeEventListener('chriptmas-user-space', clear);
      window.removeEventListener('chriptmas-device-required', clear);
      outcomeSavePending.current?.controller.abort(); outcomeSavePending.current = null;
    };
  }, [gate, scope]);
  useEffect(() => () => {
    for (const operation of stopRequests.current.values()) operation.controller.abort();
    stopRequests.current.clear();
  }, [scope]);
  useEffect(() => {
    setInterventionDraft(null);
    outcomeRedoPending.current = null;
    setError(''); setBusy(false); setReviewing(null); setRetrying(false); setRecording(false); setTracePanel(null); setCitationPanel(null); setOutputPanel(null); setDraftPanel(null); setSharePanel(null); setStreaming(null); setUnknownProject(null); setContinuation(null); setChoicePanel(null); creatingProject.current = false;
  }, [projectId, threadId, turnId]);
  useEffect(() => {
    const request = gate.issue('thread', scope);
    if (threadReadFailure === scope) setError('');
    setThreadReadFailure(null);
    async function load() {
      try {
        const list = await workbenchApi.threads(projectId, { scopeKind });
        if (!gate.isCurrent(request)) return;
        const threads = list.items || [];
        // 首页展示真实最近对话；带线程或轮次的地址继续回到原对话。
        let id = threadId || (turnId || isShared ? readStoredJson(cacheKey).value : null);
        // 空字符串记录用户主动新建的空对话，切页或刷新继续保留。
        if (!threadId && id && !threads.some(thread => thread.id === id)) id = threads[0]?.id || null;
        if (id === '') id = null;
        let detail = id ? await workbenchApi.thread(projectId, id, { scopeKind }) : { turns: [] };
        if (!gate.isCurrent(request)) return;
        if (turnId && !detail.turns.some(turn => turn.id === turnId)) {
          for (const thread of threads.filter(thread => thread.id !== id)) {
            const candidate = await workbenchApi.thread(projectId, thread.id, { scopeKind });
            if (!gate.isCurrent(request)) return;
            if (candidate.turns.some(turn => turn.id === turnId)) { id = thread.id; detail = candidate; break; }
          }
        }
        setSession({ scope, project: projectId, threads, id, turns: detail.turns });
        setThreadReadFailure(null);
        if (id) writeStoredJson(cacheKey, id);
      } catch (failure) { if (gate.isCurrent(request)) { setError(failure); setThreadReadFailure(scope); } }
    }
    load();
    return () => gate.invalidate('thread');
  }, [projectId, threadId, turnId, threadReadRetry]);
  useEffect(() => {
    if (!sessionReady || !visible.id) return;
    if (resumedTurns.current.scope !== scope) resumedTurns.current = { scope, ids: new Set() };
    const turn = [...visible.turns].reverse().find(value =>
      (value.receipt.ask?.interruption && value.receipt.ask.interruption !== 'stopped' || value.receipt.do?.state === 'interrupted')
      && !resumedTurns.current.ids.has(value.id));
    if (!turn) return;
    resumedTurns.current.ids.add(turn.id);
    const request = gate.issue('resume', scope), controller = new AbortController();
    const id = visible.id;
    streamController.current = controller; setBusy(true);
    setStreaming({ scope, ...turn, answer: (turn.receipt.ask || turn.receipt.do).partial || '' });
    let received = '';
    const text = value => { if (gate.isCurrent(request)) setStreaming(current => current?.scope === scope
      ? { ...current, answer: value, status: null } : current); };
    async function read() {
      try {
        const result = await workbenchApi.resume(projectId, turn.id, {
          scopeKind,
          signal: controller.signal,
          intent: turn.intent, executor: turn.receipt.do?.executor, kernelTurnId: turn.receipt.do?.kernel_turn_id,
          onDelta: delta => { received += delta; text(received); },
          onPartDelta: value => { if (gate.isCurrent(request)) setStreaming(current => current?.scope === scope
            ? { ...current, partAnswers: updatePartAnswer(current.partAnswers, value.part, value.text) } : current); },
          onReset: value => { if (value.part == null) { received = typeof value.text === 'string' ? value.text : ''; text(received); }
            else if (gate.isCurrent(request)) setStreaming(current => current?.scope === scope
              ? { ...current, partAnswers: updatePartAnswer(current.partAnswers, value.part, typeof value.text === 'string' ? value.text : '', true) } : current); },
          onSteer: value => { if (gate.isCurrent(request)) setStreaming(current => current?.scope === scope
            ? { ...current, steers: [...(current.steers || []).filter(entry => entry.id !== value.id), value] } : current); },
          onStatus: value => { if (gate.isCurrent(request)) setStreaming(current => current?.scope === scope
            ? { ...current, status: value.state === 'retrying' ? value : null } : current); },
          onTaskProgress: value => { if (gate.isCurrent(request)) setStreaming(current => current?.scope === scope && current.id === value.turn_id
            && turn.receipt.do?.executor === value.executor && turn.receipt.do?.kernel_turn_id === value.kernel_turn_id
            ? { ...current, external_progress: value.progress, external_usage: value.progress.usage } : current); },
        });
        if (!gate.isCurrent(request)) return;
        if (result.thread_id !== id) throw new Error('读取未完成 · 重试');
        setSession(current => ({ ...current, turns: current.turns.map(old => old.id === turn.id ? result.turn : old) }));
      } catch (failure) { if (gate.isCurrent(request)) { clearFinishedStop(failure); setError(failure); } }
      finally {
        if (streamController.current === controller) streamController.current = null;
        if (gate.isCurrent(request)) { setBusy(false); setStreaming(null); }
      }
    }
    read();
    return () => { controller.abort(); gate.invalidate('resume'); };
  }, [scope, projectId, sessionReady, visible.id, clearFinishedStop]);
  const processing = visible.turns.some(turn => busyReceipt(turn.receipt));
  useEffect(() => {
    const pending = Object.values(stops), stopping = pending[0], awaiting = stopping || pendingContinue;
    if ((!processing && !awaiting) || (!visible.id && !awaiting) || (!awaiting && (busy || reviewing || retrying || sharedRemember.working))) return;
    const id = awaiting?.threadId || visible.id, project = awaiting?.project || projectId;
    if (!id) return;
    const controller = new AbortController();
    let timer, cancelled = false;
    async function poll() {
      const request = gate.issue('poll', scope);
      try {
        const detail = await workbenchApi.thread(project, id, { scopeKind, signal: controller.signal });
        if (cancelled || !gate.isCurrent(request)) return;
        if (detail.id !== id || !Array.isArray(detail.turns)) throw new Error('读取未完成 · 重试');
        if (project === projectId) setSession(current => current.scope === scope && current.project === projectId
          && (current.id === visible.id || current.id === id) ? { ...current, id, turns: detail.turns,
            threads: current.threads.some(thread => thread.id === id) ? current.threads : [{ id, title: displayScopedText(detail.turns[0]?.user_text || '') }, ...current.threads] } : current);
        const settled = pending.filter(entry => entry.project === project && entry.threadId === id
          && settledStop(receiptTurn(detail.turns, entry.id)));
        if (settled.length) {
          setStopState(current => current.scope === scope ? { scope,
            items: Object.fromEntries(Object.entries(current.items).filter(([, entry]) => !settled.some(done => done.id === entry.id && done.project === entry.project))) } : current);
          setStreaming(current => current?.scope === scope && settled.some(entry => entry.id === current.id) ? null : current);
        }
        if (pendingContinue || pending.some(entry => !settled.includes(entry)) || detail.turns.some(turn => busyReceipt(turn.receipt))) timer = setTimeout(poll, 1500);
      } catch (failure) { if (!cancelled && gate.isCurrent(request)) {
        if (!(stopping && failure.code === 'workbench_not_found')) setError(failure);
        timer = setTimeout(poll, 1500);
      } }
    }
    timer = setTimeout(poll, 1500);
    return () => { cancelled = true; controller.abort(); clearTimeout(timer); gate.invalidate('poll'); };
  }, [projectId, threadId, turnId, visible.id, visible.turns.length, processing, busy, reviewing, retrying, stops, pendingContinue, sharedRemember.working]);
  useEffect(() => {
    if (locatedTurn.current?.scope !== scope || locatedTurn.current?.turnId !== turnId)
      locatedTurn.current = { scope, turnId, done: false };
    if (!turnId || locatedTurn.current.done) return;
    const target = document.getElementById(`workbench-${turnId}`);
    if (!target) return;
    target.scrollIntoView?.({ block: 'center' });
    // 目标挂载后只定位一次，后续回执刷新保留用户的阅读位置。
    locatedTurn.current.done = true;
  }, [scope, turnId, visible.turns]);
  useEffect(() => () => {
    startingRecord.current = false;
    const recorder = recorderRef.current;
    if (recorder) { recorder.onstop = null; if (recorder.state !== 'inactive') recorder.stop(); recorder.stream.getTracks().forEach(track => track.stop()); recorderRef.current = null; }
    recordingResolveRef.current?.(null); recordingResolveRef.current = null;
  }, [projectId, threadId, turnId]);
  async function record() {
    if (!sessionReady) return null;
    if (recording) { recorderRef.current?.stop(); setRecording(false); return; }
    if (startingRecord.current) return;
    startingRecord.current = true;
    let acquiredStream;
    try {
      if (!globalThis.MediaRecorder || !navigator.mediaDevices?.getUserMedia) throw new Error();
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      acquiredStream = stream;
      if (!gate.isScopeCurrent(scope)) { stream.getTracks().forEach(track => track.stop()); return; }
      const chunks = [], recorder = new MediaRecorder(stream);
      recorderRef.current = recorder;
      recorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data); };
      const result = new Promise(resolve => { recordingResolveRef.current = resolve; recorder.onstop = () => {
        stream.getTracks().forEach(track => track.stop()); recorderRef.current = null;
        recordingResolveRef.current = null;
        if (!gate.isScopeCurrent(scope) || !chunks.length) { resolve(null); return; }
        setRecording(false);
        const type = recorder.mimeType || 'audio/webm';
        resolve(new File(chunks, `录音-${Date.now()}.${type.includes('mp4') ? 'm4a' : 'webm'}`, { type }));
      }; });
      recorder.start(); setRecording(true); return await result;
    } catch { acquiredStream?.getTracks().forEach(track => track.stop()); recorderRef.current = null; recordingResolveRef.current?.(null); recordingResolveRef.current = null; if (gate.isScopeCurrent(scope)) { setRecording(false); setError('录音不可用 · 添加文件'); } }
    finally { startingRecord.current = false; }
  }
  function rememberAdoption() {
    return { request: gate.issue('remember-adopt', scope), threadId: visible.id };
  }
  function adoptSharedRemember(accepted, origin) {
    if (!accepted || !gate.isCurrent(origin.request) || selectedThread.current !== origin.threadId) return false;
    const { command, result } = accepted, id = result.thread_id;
    gate.invalidate('thread');
    writeStoredJson(cacheKey, id);
    setSession(current => ({
      scope, project: projectId, id,
      threads: current.threads.some(thread => thread.id === id) ? current.threads
        : [{ id, title: displayScopedText(result.turn.user_text || command.body.text) }, ...current.threads],
      turns: current.id !== id ? [result.turn]
        : current.turns.some(turn => turn.id === result.turn.id) ? current.turns : [...current.turns, result.turn],
    }));
    sharedRemember.settle(command);
    if (id !== visible.id) onNavigate?.('workbench', { project_id: projectId, scope_kind: 'shared', thread_id: id });
    else if (visible.turns.some(turn => turn.id === result.turn.id)) setThreadReadRetry(value => value + 1);
    return true;
  }
  async function runSharedTask(entry, mode) {
    if (!entry || !sessionReady || busy || retrying || sharedOperationPending()
        || (mode === 'view') !== Boolean(entry.accepted)
        || (mode === 'initial' || mode === 'continue') && projectScope?.can_do !== true
        || (mode === 'continue' || mode === 'dismiss') && entry.status !== 'not_received') return false;
    if (mode === 'dismiss' && !sharedTaskTextTooLong(entry.command.body.text)) return false;
    if (mode === 'continue' && sharedTaskTextTooLong(entry.command.body.text)) return false;
    const request = gate.issue('send', scope);
    if (!request) return false;
    const { command } = entry, originalThread = visible.id;
    let accepted = entry.accepted, submitted = false;
    const current = () => gate.isCurrent(request) && selectedThread.current === originalThread;
    const controller = new AbortController();
    streamController.current = controller;
    gate.invalidate('thread'); gate.invalidate('poll'); setBusy(true); setError('');
    function acknowledge(binding) {
      if (accepted && (binding.threadId !== accepted.threadId || binding.turnId !== accepted.turnId))
        throw new Error('原任务回执身份未核对');
      accepted = binding;
      sharedTask.acknowledge(command, binding);
    }
    function settle() {
      try { sharedTask.settle(command); } catch (failure) { setError(failure); }
    }
    function adopt(turn, detail) {
      if (!current() || !accepted || turn.id !== accepted.turnId || !turn.receipt?.do) return false;
      const id = accepted.threadId;
      sharedTaskAcceptance(command, { thread_id: id, turn });
      writeStoredJson(cacheKey, id);
      setSession(currentSession => currentSession.scope !== scope || currentSession.id !== originalThread ? currentSession : ({
        scope, project: projectId, id,
        threads: currentSession.threads.some(thread => thread.id === id) ? currentSession.threads
          : [{ id, title: displayScopedText(turn.user_text || command.body.text) }, ...currentSession.threads],
        turns: detail?.turns || (currentSession.id === id
          ? currentSession.turns.some(row => row.id === turn.id) ? replaceReceiptTurn(currentSession.turns, turn) : [...currentSession.turns, turn]
          : [turn]),
      }));
      settle();
      if (mode === 'view' || id !== originalThread)
        onNavigate?.('workbench', { project_id: projectId, scope_kind: 'shared', thread_id: id,
          ...(mode === 'view' ? { turn_id: turn.id } : {}) });
      return true;
    }
    async function readAccepted() {
      if (!accepted || !current() || controller.signal.aborted) return false;
      const detail = await workbenchApi.thread(projectId, accepted.threadId, { scopeKind: 'shared', signal: controller.signal });
      if (!current()) return false;
      const turn = detail.turns.find(row => row.id === accepted.turnId);
      if (!turn || !adopt(turn, detail)) throw new Error('原任务回执未读取 · 核对');
      return true;
    }
    async function readCommand() {
      sharedTask.observe(command, 'unknown');
      let value;
      try { value = await workbenchApi.taskCommand(projectId, command.id, { signal: controller.signal }); }
      catch (failure) {
        if (!current()) return null;
        const status = sharedTaskNotReceived(failure) ? 'not_received' : 'unknown';
        sharedTask.observe(command, status);
        if (status !== 'not_received') throw failure;
        return status;
      }
      if (!current()) return null;
      const proof = sharedTaskCommandResult(command, value);
      if (proof.state === 'accepted') acknowledge(proof.accepted);
      else if (proof.state === 'failed') { setError({ code: proof.error }); settle(); }
      else sharedTask.observe(command, proof.state);
      return proof.state;
    }
    try {
      if (mode === 'view') return await readAccepted();
      if (mode === 'check') { await readCommand(); return false; }
      if (mode === 'dismiss') {
        if (await readCommand() === 'not_received' && current()) settle();
        return false;
      }
      if (mode === 'continue' && (await readCommand()) !== 'not_received') return false;
      if (!current()) return false;
      sharedTask.observe(command, 'unknown'); submitted = true;
      const result = await workbenchApi.create(command.body, {
        submissionKey: command.id, signal: controller.signal,
        onStarted: value => {
          acknowledge(sharedTaskAcceptance(command, value));
          if (mode === 'continue') { controller.abort(); return; }
          if (current()) {
            onSharedAccepted({ projectId, threadId: accepted.threadId, turnId: accepted.turnId, actor: deviceIdentity() });
            setStreaming({ scope, ...value.turn, project: projectId, executor: 'builtin', threadId: accepted.threadId, answer: '' });
          }
        },
        onDelta: delta => { if (current()) setStreaming(value => value?.scope === scope ? { ...value, answer: value.answer + delta, status: null } : value); },
        onReset: value => { if (current()) setStreaming(stream => stream?.scope === scope
          ? { ...stream, answer: typeof value.text === 'string' ? value.text : '', status: null } : stream); },
        onStatus: status => { if (current()) setStreaming(value => value?.scope === scope
          ? { ...value, status: status.state === 'retrying' ? status : null } : value); },
      });
      acknowledge(sharedTaskAcceptance(command, result));
      return mode === 'initial' && adopt(result.turn);
    } catch (failure) {
      if (!current() || failure.name === 'AbortError') return false;
      setError(failure);
      if (submitted) try {
        if (!accepted) await readCommand();
        if (accepted && mode === 'initial') return await readAccepted();
      } catch (readFailure) { if (current()) setError(readFailure); }
      return false;
    } finally {
      if (streamController.current === controller) streamController.current = null;
      if (gate.isCurrent(request)) { setBusy(false); setStreaming(null); }
    }
  }
  async function send({ text, files, intent, onAccepted }, createdProject, preparedUpdate) {
    if (!gate.isScopeCurrent(scope) || (busy && !createdProject && !preparedUpdate) || preparingOutcome.current?.scope === scope && !preparedUpdate || !sessionReady) return false;
    if (isShared && (retrying || sharedOperationPending())) return false;
    if (isShared && intent === 'inspiration') {
      if (!projectScope?.can_write || createdProject || preparedUpdate) return false;
      try { sharedInspirationRequest(projectId, text, files); return await sharedInspirations.send(text); }
      catch (failure) { setError(failure); return false; }
    }
    if (isShared && intent === 'remember') {
      if (!projectScope?.can_write || createdProject || preparedUpdate) return false;
      try {
        const origin = rememberAdoption();
        setError('');
        return adoptSharedRemember(await sharedRemember.send(sharedRememberRequest(projectId, text, files, origin.threadId)), origin);
      } catch (failure) { setError(failure); return false; }
    }
    if (isShared && intent === 'do') {
      if (projectScope?.can_do !== true || createdProject || preparedUpdate) return false;
      try {
        const body = { ...sharedTaskRequest(projectId, text, files), ...(visible.id ? { thread_id: visible.id } : {}) };
        const command = sharedTask.prepare(body);
        return command ? await runSharedTask({ command, accepted: null }, 'initial') : false;
      } catch (failure) { setError(failure); return false; }
    }
    let sharedRequest;
    if (isShared) {
      if (!projectScope?.can_ask || createdProject || preparedUpdate) return false;
      try { sharedRequest = sharedAskRequest(projectId, text, files, intent); intent = sharedRequest.intent; }
      catch (failure) { setError(failure); return false; }
    }
    const request = gate.issue('send', scope);
    gate.invalidate('thread'); gate.invalidate('poll');
    setBusy(true); setError(''); setUnknownProject(null);
    const controller = new AbortController(); streamController.current = controller;
    let attemptedProject = projectId, acceptedTurn = null, acceptedText = false;
    const acceptedFiles = [];
    const acceptance = () => acceptedFiles.length ? (acceptedFiles.length === files.length ? true : { acceptedFiles }) : acceptedText;
    const originalThread = visible.id, originalIdentity = userStorageKey('workbench-send');
    try {
      const executor = allowExternalExecutor && !preparedUpdate && continuation?.scope !== scope && !files.length && (!intent || intent === 'do') ? await externalExecutor.validate(controller.signal) : 'builtin';
      if (!gate.isCurrent(request)) return false;
      if (executor !== 'builtin') intent = 'do';
      const defaultProject = continuation?.scope === scope && !files.length && intent === 'do'
        ? continuation.projectId || projectId : projectId;
      const project = sharedRequest?.project_id || preparedUpdate?.project_id || createdProject || targetProject(text, intent, defaultProject, projects);
      attemptedProject = project;
      const threadCacheKey = userStorageKey(`chriptmas-v2-thread:${isShared ? 'shared:' : ''}${project}`);
      let id = preparedUpdate ? preparedUpdate.threadId : project === projectId ? visible.id : null;
      const retry = attachmentRetry.current;
      if (retry?.scope === scope && retry.project === project && files.length && files.every(file => retry.files.includes(file))) id = retry.threadId;
      const images = files.length > 1 && files.every(file => /\.(png|jpe?g|bmp|tiff?|webp)$/i.test(file.name));
      const uploadBatches = images ? [files] : files.map(file => [file]);
      const routedAttachments = files.length > 0 && Boolean(scopedText(text).text);
      const routedUploads = [];
      async function uploadBatch(batch) {
        const [file, ...others] = batch;
        let uploaded = attachmentUploads.current.find(value => value.identity === originalIdentity && value.project === project
          && value.files.length === batch.length && value.files.every((entry, index) => entry === batch[index]));
        if (!uploaded) {
          uploaded = { identity: originalIdentity, project, files: batch, item: await workbenchApi.file(project, file, others) };
          attachmentUploads.current.push(uploaded);
        }
        return uploaded;
      }
      if (routedAttachments) {
        for (const batch of uploadBatches) {
          routedUploads.push(await uploadBatch(batch));
          if (!gate.isCurrent(request)) return acceptance();
        }
      }
      const batches = routedAttachments ? [files] : uploadBatches;
      if (!batches.length) batches.push([]);
      for (const batch of batches) {
        const [file] = batch;
        const uploaded = file && !routedAttachments ? await uploadBatch(batch) : null;
        const item = uploaded?.item;
        if (!gate.isCurrent(request)) return acceptance();
        let batchAccepted = false;
        const acceptBatch = () => { if (!batchAccepted) {
          acceptedFiles.push(...batch); batchAccepted = true;
          if (!routedAttachments) attachmentUploads.current = attachmentUploads.current.filter(value => value !== uploaded);
        } };
        const result = await workbenchApi.create({ project_id: project, ...(id ? { thread_id: id } : {}), text, ...(routedAttachments ? { item_ids: routedUploads.map(value => value.item.id), ...(intent ? { intent } : {}) } : file ? { intent: 'remember' } : intent ? { intent } : {}), ...(item ? { item_id: item.id } : {}),
          ...sharedRequest,
          ...(executor !== 'builtin' ? { executor } : {}),
          ...(preparedUpdate ? { executor: 'builtin', continue_from: preparedUpdate.continue_from, context_ids: preparedUpdate.context_ids } : {}),
          ...(!isShared && !preparedUpdate && executor === 'builtin' && !file && intent === 'do' && project === (continuation?.projectId || projectId) && continuation?.scope === scope ? { continue_from: continuation.documentId } : {}) }, {
          submissionKey: preparedUpdate?.submissionKey, onSubmissionSettled: preparedUpdate?.onSettled,
          signal: controller.signal,
          onStarted: value => {
            acceptBatch();
            if (!gate.isCurrent(request)) return;
            preparedUpdate?.onAccepted?.();
            setStreaming({ scope, ...value.turn, parts: value.parts, project, executor, threadId: value.thread_id, answer: '' });
            const acceptedThread = typeof value.thread_id === 'string' && value.thread_id ? value.thread_id : id;
            if (isShared && value.turn.scope_kind === 'shared' && value.turn.intent === 'ask' && acceptedThread)
              onSharedAccepted({ projectId: project, threadId: acceptedThread, turnId: value.turn.id, actor: deviceIdentity() });
            if (file && acceptedThread) attachmentRetry.current = { scope, project, threadId: acceptedThread, files: files.filter(value => !acceptedFiles.includes(value)) };
            if ((value.turn.intent === 'do' || isShared && value.turn.intent === 'ask') && acceptedThread && (project !== projectId || !originalThread || acceptedThread === originalThread))
              acceptedTurn = { project, id: acceptedThread, turnId: value.turn.id };
            if (typeof value.thread_id === 'string') writeStoredJson(threadCacheKey, value.thread_id);
          },
          onDelta: delta => { if (gate.isCurrent(request)) setStreaming(current => current?.scope === scope ? { ...current, answer: current.answer + delta, status: null } : current); },
          onPartDelta: value => { if (gate.isCurrent(request)) setStreaming(current => current?.scope === scope
            ? { ...current, partAnswers: updatePartAnswer(current.partAnswers, value.part, value.text) } : current); },
          onReset: value => { if (gate.isCurrent(request)) setStreaming(current => current?.scope !== scope ? current
            : value.part == null ? { ...current, answer: typeof value.text === 'string' ? value.text : '', status: null }
              : { ...current, partAnswers: updatePartAnswer(current.partAnswers, value.part, typeof value.text === 'string' ? value.text : '', true) }); },
          onSteer: value => { if (gate.isCurrent(request)) setStreaming(current => current?.scope === scope
            ? { ...current, steers: [...(current.steers || []).filter(entry => entry.id !== value.id), value] } : current); },
          onStatus: value => { if (gate.isCurrent(request)) setStreaming(current => current?.scope === scope ? { ...current, status: value.state === 'retrying' ? value : null } : current); },
          onTaskProgress: value => { if (gate.isCurrent(request)) setStreaming(current => current?.scope === scope && current.id === value.turn_id
            && current.project === value.project_id && executor === value.executor
            ? { ...current, external_progress: value.progress, external_usage: value.progress.usage } : current); },
        });
        if (routedAttachments) attachmentUploads.current = attachmentUploads.current.filter(value => !routedUploads.includes(value));
        acceptBatch();
        if (!gate.isCurrent(request)) return acceptance();
        id = result.thread_id;
        if (file) attachmentRetry.current = { scope, project, threadId: id, files: files.filter(value => !acceptedFiles.includes(value)) };
        writeStoredJson(threadCacheKey, id);
        const favorites = !file && (!intent || intent === 'remember') && /https:\/\/(?:space\.bilibili\.com\/\d+\/favlist|(?:www\.)?bilibili\.com\/medialist\/detail\/ml\d+)/.test(text);
        const expanded = favorites ? await workbenchApi.thread(project, id, { scopeKind }) : null;
        if (!gate.isCurrent(request)) return acceptance();
        if (project === projectId) setSession(current => ({ scope, project, id, threads: current.threads.some(thread => thread.id === id) ? current.threads : [{ id, title: displayScopedText(result.turn.user_text || file?.name || '') }, ...current.threads], turns: expanded?.turns || (current.id === id ? current.turns.some(old => old.id === result.turn.id) ? replaceReceiptTurn(current.turns, result.turn) : [...current.turns, result.turn] : [result.turn]) }));
      }
      if (!preparedUpdate) setContinuation(null);
      attachmentRetry.current = null;
      if (!preparedUpdate) externalExecutor.reset();
      if (project !== projectId || id !== visible.id) onNavigate?.('workbench', { project_id: project, thread_id: id, scope_kind: scopeKind });
      return true;
    } catch (failure) { if (gate.isCurrent(request)) {
      clearFinishedStop(failure);
      setError(failure, attemptedProject);
      if (failure.projectTag) setUnknownProject({ scope, name: failure.projectTag, input: { text, files, intent, onAccepted } });
      // 错误帧可能晚于失败回执落盘；只回读本次已接受的线程，不重发任务。
      if (acceptedTurn && !controller.signal.aborted && userStorageKey('workbench-send') === originalIdentity) {
        try {
          const detail = await workbenchApi.thread(acceptedTurn.project, acceptedTurn.id, { scopeKind });
          if (gate.isCurrent(request) && userStorageKey('workbench-send') === originalIdentity) {
            if (!detail.turns?.some(turn => turn.id === acceptedTurn.turnId)) return acceptance();
            failure.clearAcceptedRequest?.();
            acceptedText = true;
            writeStoredJson(userStorageKey(`chriptmas-v2-thread:${isShared ? 'shared:' : ''}${acceptedTurn.project}`), acceptedTurn.id);
            if (acceptedTurn.project === projectId) {
              setSession(current => current.scope === scope && current.project === projectId
                && (current.id === originalThread || current.id === acceptedTurn.id)
                ? { ...current, id: acceptedTurn.id, turns: detail.turns,
                  threads: current.threads.some(thread => thread.id === acceptedTurn.id) ? current.threads
                    : [{ id: acceptedTurn.id, title: displayScopedText(text) }, ...current.threads] } : current);
            } else {
              onAccepted?.(acceptance());
              onNavigate?.('workbench', { project_id: acceptedTurn.project, thread_id: acceptedTurn.id, scope_kind: scopeKind });
            }
          }
        } catch { /* 保留原任务错误和输入，回读失败仍可由用户刷新查看。 */ }
      }
    } return acceptance(); }
    finally { if (streamController.current === controller) streamController.current = null; if (gate.isCurrent(request)) { setBusy(false); setStreaming(null); } }
  }
  const askElsewhere = useCallback((turn, hint) => {
    if (isShared || !gate.isScopeCurrent(scope)) return;
    return send({ text: `${formatScopeTag(hint.project_id, hint.scene || null)}\n${scopedText(turn.user_text).text}`, files: [], intent: 'ask' });
  }, [busy, sessionReady, gate, scope, projectId, projects, visible.id, onNavigate, isShared]);
  async function createUnknownProject() {
    if (!unknownProject || unknownProject.scope !== scope || creatingProject.current || busy) return;
    const retained = unknownProject, request = gate.issue('project', scope);
    creatingProject.current = true; setBusy(true); setError('');
    try {
      const project = retained.project || await workbenchApi.createProject(retained.name);
      if (!gate.isCurrent(request)) return;
      setUnknownProject({ ...retained, project }); onProjectCreated?.(project);
      const accepted = await send(retained.input, project.id);
      retained.input.onAccepted?.(accepted);
      if (gate.isCurrent(request)) {
        if (accepted === true) setComposerRevision(value => value + 1);
        else if (accepted?.acceptedFiles?.length) setUnknownProject(null);
        else setUnknownProject({ ...retained, project });
      }
    } catch (failure) { if (gate.isCurrent(request)) setError(failure); }
    finally { if (gate.isCurrent(request)) { creatingProject.current = false; setBusy(false); } }
  }
  const refreshPlacement = useCallback(async () => {
    if (!visible.id) return;
    const request = gate.issue('placement', scope);
    try {
      const detail = await workbenchApi.thread(projectId, visible.id, { scopeKind });
      if (gate.isCurrent(request)) setSession(current => ({ ...current, turns: detail.turns }));
    } catch (failure) { if (gate.isCurrent(request)) setError(failure); }
  }, [gate, scope, projectId, visible.id]);
  const review = useCallback(async (insight, action, project = projectId) => {
    if (reviewing) return;
    const request = gate.issue('review', scope); setReviewing(insight.id); setError('');
    gate.invalidate('thread'); gate.invalidate('poll');
    try {
      const updated = await workbenchApi.insight(project, insight, action);
      if (!gate.isCurrent(request)) return;
      // 审核结果也同步到安静终态的子回执，保留各部分的归属与其他结果。
      const updateReceipt = receipt => ({ ...receipt,
        ...(receipt.remember && { remember: { ...receipt.remember, insights: receipt.remember.insights.flatMap(old => old.id !== insight.id ? [old] : action === 'drop' ? [] : [updated]) } }),
        ...(receipt.inspiration && { inspiration: { ...receipt.inspiration, insight: receipt.inspiration.insight?.id === insight.id ? (action === 'drop' ? null : updated) : receipt.inspiration.insight } }),
        ...(receipt.parts && { parts: receipt.parts.map(part => part.receipt ? { ...part, receipt: updateReceipt(part.receipt) } : part) }),
      });
      setSession(current => ({ ...current, turns: current.turns.map(turn => ({ ...turn, receipt: updateReceipt(turn.receipt) })) }));
    } catch (failure) { if (gate.isCurrent(request)) setError(failure); }
    finally { if (gate.isCurrent(request)) setReviewing(null); }
  }, [reviewing, gate, scope, projectId]);
  const retry = useCallback(async (turn, project = projectId) => {
    if (retrying || isShared && (busy || sharedOperationPending() || selectedThread.current !== visible.id
        || !projectScope?.can_write || turn.scope_kind !== 'shared' || turn.intent !== 'remember')) return;
    const request = gate.issue('retry', scope);
    if (!request) return;
    if (isShared) sharedRetry.current = request;
    const currentThread = () => gate.isCurrent(request) && (!isShared || selectedThread.current === visible.id);
    gate.invalidate('thread'); gate.invalidate('poll'); setRetrying(true);
    try {
      const updated = await workbenchApi.retry(project, turn.id);
      if (!currentThread()) return;
      if (isShared) {
        sharedRememberResult({ thread_id: visible.id, turn: updated }, project, visible.id);
        if (updated.id !== turn.id) throw new Error('原记住记录未核对 · 重读');
      }
      setSession(current => isShared && current.id !== visible.id ? current
        : { ...current, turns: replaceReceiptTurn(current.turns, updated) });
    }
    catch (failure) {
      if (!currentThread()) return;
      setError(failure);
      if (isShared && visible.id) {
        try {
          const detail = await workbenchApi.thread(project, visible.id, { scopeKind: 'shared' });
          if (!currentThread()) return;
          const retained = detail.turns.find(value => value.id === turn.id);
          if (!retained) return;
          setSession(current => current.id === visible.id ? { ...current, turns: detail.turns } : current);
          if (['processing', 'done'].includes(retained.receipt.remember?.state)) setError('');
        } catch {
          if (currentThread()) setThreadReadFailure(scope);
        }
      }
    }
    finally {
      if (sharedRetry.current === request) sharedRetry.current = null;
      if (gate.isCurrent(request)) setRetrying(false);
    }
  }, [retrying, gate, scope, projectId, isShared, projectScope?.can_write, visible.id, setError, busy, sharedOperationPending]);
  const stopTurn = useCallback(async (turn, project = projectId) => {
    if (isShared && (turn.scope_kind !== 'shared' || project !== projectId || !['ask', 'do'].includes(turn.intent)
      || (turn.executor || turn.receipt?.do?.executor || 'builtin') !== 'builtin')) return;
    const key = `${project}:${turn.id}`;
    if (stops[key] || stopRequests.current.has(key) || !gate.isScopeCurrent(scope)) return;
    const threadId = turn.thread_id || turn.threadId || visible.id;
    if (!threadId) return;
    const request = gate.issue(`stop:${key}`, scope), controller = new AbortController();
    const operation = { request, controller };
    stopRequests.current.set(key, operation); setError('');
    setStopState(current => ({ scope, items: { ...(current.scope === scope ? current.items : {}), [key]: { id: turn.id, project, threadId } } }));
    try { await workbenchApi.stop(project, turn.id, { signal: controller.signal }); }
    catch (failure) {
      if (!controller.signal.aborted && gate.isCurrent(request)) {
        setError(failure, project);
        setStopState(current => current.scope === scope ? { scope,
          items: Object.fromEntries(Object.entries(current.items).filter(([name]) => name !== key)) } : current);
      }
    } finally { if (stopRequests.current.get(key) === operation) stopRequests.current.delete(key); }
  }, [gate, scope, projectId, visible.id, stops, setError, isShared]);
  const continueTurn = useCallback(async (turn, project = projectId) => {
    if (isShared || busy || retrying || reviewing) return;
    const request = gate.issue('continue', scope);
    gate.invalidate('thread'); gate.invalidate('poll'); setBusy(true); setError('');
    const controller = new AbortController(); streamController.current = controller;
    // 请求等待期间只读取后端投影；收到真实运行回执后才重新提供停止。
    setContinueState({ scope, project, threadId: turn.thread_id || visible.id, id: turn.id });
    try {
      const updated = await workbenchApi.continue(project, turn.id, { signal: controller.signal });
      if (gate.isCurrent(request)) setSession(current => ({ ...current,
        turns: replaceReceiptTurn(current.turns, updated) }));
    } catch (failure) { if (gate.isCurrent(request)) { clearFinishedStop(failure); setError(failure); } }
    finally {
      if (streamController.current === controller) streamController.current = null;
      if (gate.isCurrent(request)) { setContinueState(null); setBusy(false); }
    }
  }, [busy, retrying, reviewing, gate, scope, projectId, visible.id, clearFinishedStop, isShared]);
  const closeCitation = useCallback(() => { gate.invalidate('citation'); setCitationPanel(null); }, [gate]);
  const closeOutput = useCallback(() => { gate.invalidate('output'); setOutputPanel(null); }, [gate]);
  const closeChoices = useCallback(() => { gate.invalidate('outcome-choices'); setChoicePanel(null); }, [gate]);
  const openOutput = useCallback(async (task, targetProject = projectId) => {
    gate.invalidate('share'); setSharePanel(null);
    setDraftPanel(null); setTracePanel(null); closeCitation(); closeChoices();
    const request = gate.issue('output', scope);
    if (!request) return;
    const identity = deviceIdentity(), current = () => gate.isCurrent(request) && isDeviceIdentity(identity);
    setOutputPanel({ scope, request, identity, project: targetProject, documentId: task.document_id, loading: true, qualified: false });
    // 正文独立打开；只有当前成果链才授予接着写，迟到的资格不能进入新范围。
    libraryApi.outcomeVersions(targetProject, task.document_id).then(result => {
      const qualified = Array.isArray(result?.items) && result.items.some(row => row.document_id === task.document_id && Number.isSafeInteger(row.version) && row.version > 0);
      if (current()) setOutputPanel(value => value?.scope === scope && value.documentId === task.document_id ? { ...value, qualified, previous: result.previous } : value);
    }).catch(() => {});
    try {
      const document = await recognitionApi.loadDocument({ documentId: task.document_id, projectId: targetProject });
      if (current()) {
        setOutputPanel(value => ({ ...value, scope, project: targetProject, documentId: task.document_id, document, task, loading: false }));
        libraryApi.opened(targetProject, 'document', task.document_id);
      }
    } catch { if (current()) setOutputPanel({ scope, error: true }); }
  }, [gate, scope, projectId, closeCitation, closeChoices]);
  const saveAnswerOutcome = useCallback(async (turn, targetProject = projectId) => {
    const ask = turn.receipt?.ask;
    if (outcomeSavePending.current || busy || retrying || reviewing || !sessionReady || turn.intent !== 'ask'
      || typeof ask?.answer !== 'string' || !ask.answer.trim() || ask.error || ask.interruption) return;
    const token = gate.issue('save-outcome', scope);
    if (!token) return;
    const identity = deviceIdentity(), controller = new AbortController();
    const operation = { token, identity, controller };
    const current = () => gate.isCurrent(token) && isDeviceIdentity(identity) && outcomeSavePending.current === operation;
    outcomeSavePending.current = operation;
    setSaveOutcomeState({ scope, identity, project: targetProject, turnId: turn.id }); setError('');
    try {
      const result = await workbenchApi.saveOutcome(targetProject, turn.id, { signal: controller.signal });
      if (!current()) return;
      if (typeof result?.document_id !== 'string' || !result.document_id) throw new Error('invalid_saved_outcome');
      await openOutput({ document_id: result.document_id }, targetProject);
    } catch (failure) {
      if (current() && failure.name !== 'AbortError') setError({ message: failure.status === 409
        ? '内容已变化 · 刷新' : '保存未完成 · 重试' }, targetProject);
    } finally {
      if (outcomeSavePending.current === operation) outcomeSavePending.current = null;
      if (gate.isCurrent(token) && isDeviceIdentity(identity)) setSaveOutcomeState(null);
    }
  }, [gate, scope, projectId, busy, retrying, reviewing, sessionReady, openOutput, setError]);
  const openCitation = useCallback(async (citation, origin = 'answer') => {
    gate.invalidate('share'); setSharePanel(null);
    setDraftPanel(null);
    if (!['insight', 'summary', 'note', 'source', 'inspiration'].includes(citation.layer) || !citation.id) return;
    closeOutput(); closeChoices();
    if (origin !== 'context') setTracePanel(null);
    const request = gate.issue('citation', scope), project = citation.persona ? 'me' : citation.project || projectId, signalToken = signalEpoch();
    setCitationPanel({ scope, layer: citation.layer, citation, origin, project, loading: true });
    if (citation.layer === 'source') {
      const [drill, full] = await Promise.allSettled([
        libraryApi.drill(project, 'source', citation.id), libraryApi.sourceText(project, citation.id),
      ]);
      if (!gate.isCurrent(request)) return;
      const original = full.status === 'fulfilled' && typeof full.value.text === 'string' ? full.value : null;
      setCitationPanel({ scope, layer: citation.layer, citation, origin, project, data: drill.status === 'fulfilled' ? drill.value : null, original, error: !original });
    } else {
      try {
        const data = await libraryApi.drill(project, citation.layer === 'summary' ? 'note' : citation.layer, citation.id);
        if (gate.isCurrent(request)) {
          setCitationPanel({ scope, layer: citation.layer, citation, origin, data, project, signalToken });
          libraryApi.opened(project, citation.layer, citation.id);
        }
      } catch { if (gate.isCurrent(request)) setCitationPanel({ scope, layer: citation.layer, citation, origin, error: true }); }
    }
  }, [gate, scope, projectId, closeOutput, closeChoices]);
  useEffect(() => {
    const insight = citationPanel?.data?.insight;
    if (citationPanel?.scope === scope && citationPanel.layer === 'insight' && !citationPanel.loading && !citationPanel.error && insight?.state === 'pending')
      sendSignal({ kind: 'view', project_id: citationPanel.project, object: { kind: 'insight', id: insight.id, revision: insight.revision } }, citationPanel.signalToken);
  }, [citationPanel, scope]);
  const reloadOutputPrevious = useCallback(async () => {
    const token = outputPanel?.request, documentId = outputPanel?.documentId;
    if (!gate.isCurrent(token) || outputPanel?.scope !== scope || !documentId || !isDeviceIdentity(outputPanel.identity)) throw new Error('previous_scope_changed');
    const result = await libraryApi.outcomeVersions(outputPanel.project || projectId, documentId);
    if (!gate.isCurrent(token) || !isDeviceIdentity(outputPanel.identity)) throw new Error('previous_scope_changed');
    const qualified = Array.isArray(result?.items) && result.items.some(row => row.document_id === documentId && Number.isSafeInteger(row.version) && row.version > 0);
    if (!qualified) throw new Error('previous_unavailable');
    setOutputPanel(current => current?.request === token ? { ...current, qualified, previous: result.previous } : current);
    return result.previous;
  }, [gate, scope, projectId, outputPanel]);
  const adoptSharedRedo = useCallback(result => {
    if (!gate.isScopeCurrent(scope) || selectedThread.current !== result.thread_id) return;
    setSession(current => current.scope !== scope || current.id !== result.thread_id ? current : {
      ...current, turns: current.turns.some(row => row.id === result.turn.id)
        ? replaceReceiptTurn(current.turns, result.turn) : [...current.turns, result.turn],
    });
  }, [gate, scope]);
  const redoOutput = useCallback(async (turn, selection, savedCommand) => {
    const originalThread = visible.id;
    if (outcomeRedoPending.current || isShared && (busy || retrying || sharedOperationPending()
      || projectScope?.can_do !== true || turn.scope_kind !== 'shared' || turn.intent !== 'do'
      || !originalThread || turn.thread_id !== originalThread || selectedThread.current !== originalThread || selection !== undefined)) return;
    const token = gate.issue('outcome-redo', scope);
    if (!token) return;
    outcomeRedoPending.current = token;
    const currentThread = () => gate.isCurrent(token) && (!isShared || selectedThread.current === originalThread);
    if (isShared) { gate.invalidate('thread'); gate.invalidate('poll'); }
    setRetrying(true); setError('');
    try {
      if (isShared) {
        let command = savedCommand;
        if (command) {
          if (command.project !== projectId || command.turnId !== turn.id) throw new Error('原重做范围未核对');
          const proof = await workbenchApi.checkSharedRedo(command, originalThread, currentThread);
          if (proof?.state !== 'not_received') return proof;
        } else {
          if (workbenchApi.pendingSharedRedo(projectId, turn.id)) throw new Error('请先核对原重做编号');
          const division = await workbenchApi.division(projectId, turn.id, { for_redo: true });
          if (!currentThread()) return;
          command = workbenchApi.prepareSharedRedo(projectId, turn.id, sharedTaskRedoRevision(division));
        }
        if (!currentThread()) return;
        const result = await workbenchApi.submitSharedRedo(command, originalThread);
        if (currentThread()) {
          adoptSharedRedo(result);
          workbenchApi.settleSharedRedo(command);
        }
        return;
      }
      const division = await workbenchApi.division(projectId, turn.id, {for_redo:true});
      if (!currentThread()) return;
      const result = await workbenchApi.redo(projectId, turn.id, division.revision, { source_kind: division.source_kind,
        ...(selection !== undefined ? {continue_from:selection} : {}) });
      if (currentThread()) {
        gate.invalidate('outcome-choices'); setChoicePanel(null);
        setSession(current => ({
          ...current, id: result.thread_id,
          turns: current.turns.some(row => row.id === result.turn.id) ? current.turns : [...current.turns, result.turn],
        }));
      }
    } catch (failure) {
      if (currentThread()) setError(isShared && failure.code === 'turn_interrupted' ? '原重做中断 · 核对原编号' : failure);
    } finally { if (outcomeRedoPending.current === token) outcomeRedoPending.current = null; if (gate.isCurrent(token)) setRetrying(false); }
  }, [gate, scope, projectId, retrying, busy, isShared, visible.id, projectScope?.can_do, sharedOperationPending, setError, adoptSharedRedo]);
  const chooseOutput = useCallback(async (turn) => {
    gate.invalidate('share'); setSharePanel(null);
    const token = gate.issue('outcome-choices', scope);
    if (!token) return;
    closeOutput(); setTracePanel(null); closeCitation(); setDraftPanel(null);
    setChoicePanel({ scope, turn, loading: true });
    try {
      const result = await libraryApi.outcomeChoices(projectId);
      if (gate.isCurrent(token)) setChoicePanel({ scope, turn, items: result.items });
    } catch { if (gate.isCurrent(token)) setChoicePanel({ scope, turn, error: true }); }
  }, [gate, scope, projectId, closeOutput, closeCitation]);
  async function updateOutcome(changes) {
    if (busy || preparingOutcome.current?.scope === scope || retrying || reviewing || !sessionReady || outputPanel?.scope !== scope || !outputPanel.qualified
      || !isDeviceIdentity(outputPanel.identity)) return false;
    const project = outputPanel.project || projectId, documentId = outputPanel.documentId;
    const slot = userStorageKey(`outcome-update:${project}:${documentId}`);
    const request = gate.issue('outcome-update', scope);
    preparingOutcome.current = { scope, request }; setBusy(true);
    try {
      let action = readStoredJson(slot).value;
      if (!action) {
        action = { key: `outcome-update-${crypto.randomUUID()}`, changes: { ack_revision: changes.ack_revision, observation: changes.observation },
          threadId: project === projectId ? visible.id : null, prepared: null };
        if (!writeStoredJson(slot, action)) throw new Error('无法保存成果更新编号');
      }
      if (typeof action.key !== 'string' || !(action.threadId === null || typeof action.threadId === 'string')) throw new Error('成果更新记录无效');
      if (!action.prepared) {
        let response;
        try { response = await libraryApi.prepareOutcomeUpdate(project, documentId, action.changes, `${action.key}-prepare`); }
        catch (failure) {
          if (['invalid_outcome_update_key', 'invalid_outcome_change_request', 'outcome_observation_changed', 'outcome_sources_unverifiable'].includes(failure.code)
            && readStoredJson(slot).value?.key === action.key) removeStoredItem(slot);
          throw failure;
        }
        const prepared = readOutcomeUpdate(response, project, documentId);
        if (!gate.isCurrent(request) || !isDeviceIdentity(outputPanel.identity)) return false;
        action = { ...action, prepared };
        if (!writeStoredJson(slot, action)) throw new Error('无法保存成果更新准备');
      }
      const prepared = readOutcomeUpdate(action.prepared, project, documentId);
      const onSettled = () => {
        if (readStoredJson(slot).value?.key === action.key) removeStoredItem(slot);
      };
      const onAccepted = () => setOutputPanel(current => current?.scope === scope && current.request === outputPanel.request ? null : current);
      const accepted = await send({ text: prepared.text, files: [], intent: 'do' }, undefined,
        { ...prepared, threadId: action.threadId, submissionKey: action.key, onSettled, onAccepted });
      if (accepted === true) { onAccepted(); return true; }
      return { accepted: false, pending: readStoredJson(slot).value?.key === action.key };
    } finally {
      if (preparingOutcome.current?.request === request) preparingOutcome.current = null;
      if (gate.isCurrent(request)) setBusy(false);
    }
  }
  function continueOutput() {
    if (!gate.isScopeCurrent(scope) || outputPanel?.scope !== scope || !outputPanel.documentId || !outputPanel.qualified
      || !isDeviceIdentity(outputPanel.identity)) return;
    externalExecutor.reset(); externalExecutor.onClose();
    const targetProject = outputPanel.project || projectId;
    const projectName = projects.find(project => project.id === targetProject)?.name
      || ({ default: '日常', inbox: '收件箱', me: '我' })[targetProject];
    setContinuation({ scope, id: ++continuationSequence.current, documentId: outputPanel.documentId, projectId: outputPanel.project || projectId,
      text: `${targetProject !== projectId && projectName ? formatScopeTag(projectName) + '\n' : ''}接着《${outputPanel.document.title || outputPanel.task.title}》写：`, intent: 'do' });
    closeOutput();
  }
  const closeShare = useCallback(() => { gate.invalidate('share'); setSharePanel(null); }, [gate]);
  const openShare = useCallback(async (turn, targetProject = projectId) => {
    if (isShared && (!serverConversationShares || turn.scope_kind !== 'shared')) return;
    closeChoices();
    const request = gate.issue('share', scope); if (!request) return;
    closeOutput(); closeCitation(); setTracePanel(null); setDraftPanel(null);
    const source = serverConversationShares ? { turnId: turn.id, projectId: targetProject, scopeKind } : null;
    const selection = { scope, turn, source, targetProject };
    if (isShared) { setSharePanel({ ...selection, remote: true }); return; }
    setSharePanel({ ...selection, loading: true });
    try {
      const receipt = turn.receipt;
      let answer, citations = [];
      if (receipt.ask) { answer = receipt.ask.answer; citations = receipt.ask.citations || []; }
      else if (receipt.inspiration?.insight) answer = receipt.inspiration.insight.text;
      else {
        const documentId = receipt.do?.document_id || receipt.remember?.document_id;
        if (!documentId) throw new Error('分享正文未完成');
        const document = await recognitionApi.loadDocument({ documentId, projectId: targetProject });
        if (!gate.isCurrent(request)) return;
        if (typeof document?.markdown !== 'string' || !Number.isInteger(document.revision) || document.revision < 1
          || (document.document_id || document.id) !== documentId) throw new Error('分享正文未读取');
        answer = document.markdown;
      }
      const snapshot = createConversationSnapshot({ question: displayScopedText(turn.user_text || ''), answer, citations });
      if (gate.isCurrent(request)) setSharePanel(current => current?.scope === scope && current.turn === turn
        ? { ...current, loading: false, snapshot } : current);
    } catch { if (gate.isCurrent(request)) setSharePanel(current => current?.scope === scope && current.turn === turn
      ? { ...current, loading: false, error: true } : current); }
  }, [gate, scope, projectId, scopeKind, isShared, serverConversationShares, closeOutput, closeCitation, closeChoices]);
  // 日常 is created by the first remembered item and automatic filing can create
  // a project; reload the list once per settled item so receipts show names, not ids.
  const memories = visible.turns.flatMap(turn => [turn.receipt?.remember, ...(turn.receipt?.parts || []).map(part => part.receipt?.remember)]).filter(Boolean);
  const unnamed = [...new Set([projectId, ...memories.map(memory => memory.filing?.target_project_id)])]
    .filter(id => id && !projects.some(project => project.id === id)).sort().join(',');
  const projectsStale = unnamed && `${unnamed}|${memories.filter(memory => memory.state !== 'processing').length}`;
  const projectsAsked = useRef('');
  useEffect(() => {
    if (!isShared && projectsStale && projectsAsked.current !== projectsStale) { projectsAsked.current = projectsStale; onProjectsStale?.(); }
  }, [projectsStale, onProjectsStale, isShared]);
  const openCorrection = useCallback((turn, project, initialIntent) => {
    closeShare(); closeOutput(); closeCitation(); closeChoices();
    setTracePanel(null); setDraftPanel(null);
    setCorrectionPanel({ scope, turnId: turn.id, projectId: project, initialIntent });
  }, [scope, closeShare, closeOutput, closeCitation, closeChoices]);
  const correctionCompleted = useCallback(async proof => {
    await refreshPlacement();
    if (proof?.replacement_turn_id) requestAnimationFrame(() => {
      document.getElementById(`workbench-${proof.replacement_turn_id}`)?.scrollIntoView({ block: 'nearest' });
    });
  }, [refreshPlacement]);
  const turnActions = useMemo(() => ({ readOnly: isShared, review, retry, continueTurn, openOutput, openCitation,
    allowSharedFiling: serverConversationShares,
    openShare: isShared && !serverConversationShares ? undefined : openShare, redoOutput, chooseOutput, closeOutput, closeCitation, saveAnswerOutcome, savingOutcome, openCorrection,
    rememberActions: isShared ? { projectId, canRead: projectScope?.can_read_sources === true,
      canRetry: projectScope?.can_write === true, disabled: busy || retrying || sharedRemember.working || sharedInspirations.working, retry, onNavigate, openSettings } : undefined,
    taskActions: isShared ? { projectId, scope, openSettings, canRead: projectScope?.can_read_sources === true,
      canDo: projectScope?.can_do === true, disabled: busy || retrying || sharedRemember.working || sharedInspirations.working,
      redo: redoOutput, acceptRedo: adoptSharedRedo,
      renderRetry: turn => <TaskRetryAction task={turn.receipt.do} turn={turn} projectId={projectId} scope={scope}
        disabled={busy || retrying || sharedRemember.working || sharedInspirations.working} onRetry={() => redoOutput(turn)} shared/>,
    } : undefined,
    setTracePanel: value => { if (value) { closeShare(); closeChoices(); } setTracePanel(value); },
    setDraftPanel: value => { if (value) { closeShare(); closeChoices(); } setDraftPanel(value); },
    setSession, onNavigate, openSettings, askElsewhere, projects, refreshPlacement, setError, stopTurn, stops, retrying, sending: busy || !sessionReady }),
  [review, retry, continueTurn, openOutput, openCitation, openShare, redoOutput, adoptSharedRedo, chooseOutput, closeOutput, closeCitation, closeShare, closeChoices, onNavigate, openSettings, askElsewhere, projects, refreshPlacement, setError, stopTurn, stops, retrying, busy, sessionReady, saveAnswerOutcome, savingOutcome, openCorrection, isShared, serverConversationShares, scope, projectId, projectScope?.can_read_sources, projectScope?.can_write, projectScope?.can_do, sharedRemember.working, sharedInspirations.working]);
  const currentStream = streaming?.scope === scope && !stoppedTurn(receiptTurn(visible.turns, streaming.id))
    && !receiptTurn(visible.turns, streaming.id)?.receipt?.ask?.error ? streaming : null;
  const streamProject = currentStream?.project || projectId;
  const streamStopping = Boolean(currentStream && stops[`${streamProject}:${currentStream.id}`]);
  const canStopStream = currentStream && (!isShared || currentStream.scope_kind === 'shared' && streamProject === projectId)
    && ['ask', 'do'].includes(currentStream.intent)
    && (currentStream.executor || currentStream.receipt?.do?.executor || 'builtin') === 'builtin';
  const empty = !visible.turns.length && !currentStream && !(isShared && sharedInspirations.commands.length);
  const panelTurns = visible.turns.flatMap(turn => [turn, ...(turn.receipt.parts || []).filter(part => part.receipt).map(part => ({
    ...part, receipt: part.receipt, projectId: part.project_id || projectId,
  }))]);
  const lastAsk = [...panelTurns].reverse().find(turn => turn.receipt.ask);
  const lastDraft = [...panelTurns].reverse().find(turn => turn.receipt.remember?.document_id);
  const lastOutcome = [...panelTurns].reverse().find(turn => turn.receipt.do?.document_id);
  const panelNavigation = active => <nav className="workbench-panel-tabs" aria-label="聚焦视图">
    <button type="button" aria-current={active === 'context' ? 'page' : undefined} disabled={!lastAsk && tracePanel?.scope !== scope} onClick={() => {
      closeOutput(); closeCitation(); setDraftPanel(null);
      setTracePanel(current => current?.scope === scope ? current : { scope, projectId: lastAsk.projectId || projectId, ask: lastAsk.receipt.ask });
    }}>本次用了什么</button>
    <button type="button" aria-current={active === 'draft' ? 'page' : undefined} disabled={!lastDraft && draftPanel?.scope !== scope} onClick={() => {
      closeOutput(); closeCitation(); setTracePanel(null);
      setDraftPanel(current => current?.scope === scope ? current : { scope, projectId: lastDraft.projectId || projectId,
        documentId: lastDraft.receipt.remember.document_id, itemId: lastDraft.receipt.remember.item_id });
    }}>整理稿</button>
    <button type="button" aria-current={active === 'outcome' ? 'page' : undefined} disabled={!lastOutcome && outputPanel?.scope !== scope} onClick={() => {
      if (lastOutcome) openOutput(lastOutcome.receipt.do, lastOutcome.projectId || projectId);
    }}>成果</button>
  </nav>;
  const openExecutor = () => { closeShare(); closeCitation(); closeOutput(); setDraftPanel(null); setTracePanel(null); closeChoices(); externalExecutor.onOpen(); };
  const proposedInput = inputProposal?.text ? { id: inputProposal.id, intent: 'ask', confirmReplace: true,
    text: isShared ? inputProposal.text : `${formatScopeTag(projectId, inputProposal.scene || null)}\n${inputProposal.text}` } : null;
  const composerProject = continuation?.scope === scope ? continuation.projectId || projectId : projectId;
  const sharedIntents = isShared ? [...(projectScope?.can_ask ? ['ask'] : []),
    ...(projectScope?.can_write ? ['inspiration', 'remember'] : []), ...(projectScope?.can_do === true ? ['do'] : [])] : undefined;
  const pendingTask = sharedTask.visible?.entry;
  const pendingTaskTooLong = pendingTask && sharedTaskTextTooLong(pendingTask.command.body.text);
  function recoverSharedTask(mode) {
    if (!pendingTask || busy || retrying || sharedOperationPending()) return;
    const latest = sharedTask.refresh();
    if (latest?.command.id !== pendingTask.command.id || Boolean(latest.accepted) !== Boolean(pendingTask.accepted)) return;
    return runSharedTask(latest, mode);
  }
  function startConversation() {
    if (busy || reviewing || retrying || isShared && sharedOperationPending() || !sessionReady) return;
    for (const channel of ['thread', 'poll', 'outcome-choices', 'project', 'remember-adopt', 'outcome-redo']) gate.invalidate(channel);
    setTracePanel(null); closeCitation(); closeOutput(); closeShare(); setDraftPanel(null); setCorrectionPanel(null);
    setContinuation(null); setChoicePanel(null); setUnknownProject(null); setError('');
    setContinuationReset(value => value + 1);
    externalExecutor.onClose(); attachmentRetry.current = null;
    writeStoredJson(cacheKey, '');
    setSession(current => ({ ...current, id: null, turns: [] }));
    onNavigate?.('workbench', { project_id: projectId, scope_kind: scopeKind });
  }
  useEffect(() => {
    if (externalExecutor.open && [sharePanel, draftPanel, tracePanel, citationPanel, outputPanel, choicePanel].some(panel => panel?.scope === scope)) externalExecutor.onClose();
  }, [externalExecutor.open, sharePanel, draftPanel, tracePanel, citationPanel, outputPanel, choicePanel, scope]);
  return <div className="workbench-layout"><main className={`workbench ${empty ? 'is-empty' : ''}`} aria-label="工作台">
    {!!visible.threads.length && <div className="workbench-history"><select aria-label="对话" value={visible.id || ''} onChange={event => {
      const target = event.target.value;
      if (isShared && onNavigate && target !== visible.id) {
        for (const channel of ['remember-adopt', 'outcome-redo', 'send', 'retry']) gate.invalidate(channel);
        streamController.current?.abort();
      }
      onNavigate?.('workbench', { thread_id: target });
    }}>{!visible.id && <option value="">新对话</option>}{visible.threads.map(thread => <option key={thread.id} value={thread.id}>{displayScopedText(thread.title)}</option>)}</select><button type="button" aria-label="新对话" title="新对话" disabled={busy || Boolean(reviewing) || retrying || sharedRemember.working || sharedInspirations.working || !sessionReady} onClick={startConversation}>＋</button></div>}
    {isShared && <p role="status">{projectScope ? (projectScope.can_write ? '共享项目' : '共享 · 只看') : '共享资格已失效 · 本人对话保留'}</p>}
    {empty ? <h1>{isShared ? '想问共享项目里的什么？' : '今天想记住什么？'}</h1> : <div className="workbench-thread">
      {visible.turns.filter(turn => !(currentStream?.id === turn.id && turn.intent === 'ask')).map(turn => <WorkbenchTurn key={turn.id} turn={turn} turnId={turnId} projectId={projectId} scope={scope} reviewing={reviewing} continuing={busy || retrying || Boolean(reviewing)} streamText={currentStream?.id === turn.id && turn.intent === 'do' ? currentStream.answer : undefined} streamStatus={currentStream?.id === turn.id ? currentStream.status : null} streamProgress={currentStream?.id === turn.id ? currentStream.external_progress : null} actions={turnActions}/>)}
      {currentStream && !visible.turns.some(turn => turn.id === currentStream.id && turn.intent === 'do') && <article aria-busy="true">
        {currentStream.user_text && <div className="workbench-bubble">{displayScopedText(currentStream.user_text)}</div>}
        <Receipt kind={currentStream.intent || 'ask'}><div className="workbench-receipt-title"><StatusDot state="processing"/>
          {canStopStream && <StopButton disabled={streamStopping} onClick={() => stopTurn(currentStream, streamProject)}/>}
        </div><RetryCountdown status={currentStream.status}/>{currentStream.executor && currentStream.executor !== 'builtin' && <ExternalTaskProgress receipt={{ executor: currentStream.executor, external_progress: currentStream.external_progress, external_usage: currentStream.external_usage }}/>} {currentStream.intent !== 'multi' && <CitedAnswer answer={currentStream.answer}/>}</Receipt>
        {currentStream.intent === 'multi' && (currentStream.parts || currentStream.receipt?.parts || []).filter(part => part.intent === 'ask').map(part => <section key={part.index} aria-label={`第${part.index + 1}部分回答`}><Receipt kind="ask">
          <span className="ui-count">{part.span}</span>{Object.hasOwn(currentStream.partAnswers || {}, part.index) && <CitedAnswer answer={currentStream.partAnswers[part.index]}/>}
        </Receipt></section>)}
      </article>}
      {isShared && <SharedInspirationReceipts state={sharedInspirations} projectId={projectId} canWrite={projectScope?.can_write}
        onNavigate={onNavigate} retryDisabled={busy || retrying || sharedRemember.working}
        retry={command => { if (!sharedOperationPending()) return sharedInspirations.retry(command); }}/>}
    </div>}
    <div className="workbench-composer"><WorkbenchError error={error} projectId={error?.projectId} onSettings={onNavigate ? openSettings : undefined} onRetry={threadReadFailure === scope ? retryThreadRead : undefined} readFailure={threadReadFailure === scope} retryDisabled={busy || Boolean(reviewing) || retrying}/>{unknownProject?.scope === scope && <button type="button" className="workbench-project-action" disabled={busy || !sessionReady} onClick={createUnknownProject}>{unknownProject.project ? '重发到' : '新建'} #{unknownProject.name}</button>}
      {isShared && sharedInspirations.error && !sharedInspirations.errorId && <div className="workbench-error" role="alert"><span>{sharedInspirations.error}</span><button type="button" disabled={sharedInspirations.working} onClick={sharedInspirations.reload}>核对</button></div>}
      {isShared && pendingTask && <div className="workbench-error" role="status" title={pendingTask.command.body.text.slice(0, 80)}>
        <span>{{ accepted: '原任务已受理', preparing: '原任务正在准备', unknown: '原任务结果待核对',
          not_received: pendingTaskTooLong ? '原任务未受理 · 文字超出上限' : '原任务未受理' }[pendingTask.status]}</span>
        <button type="button"
          disabled={busy || retrying || sharedRemember.working || sharedInspirations.working || !sessionReady}
          onClick={() => recoverSharedTask(pendingTask.accepted ? 'view' : 'check')}>{pendingTask.accepted ? '查看原回执' : '核对'}</button>
        {pendingTask.status === 'not_received' && !pendingTaskTooLong && projectScope?.can_do === true && <button type="button"
          disabled={busy || retrying || sharedRemember.working || sharedInspirations.working || !sessionReady}
          onClick={() => recoverSharedTask('continue')}>继续原请求</button>}
        {pendingTask.status === 'not_received' && pendingTaskTooLong && <button type="button"
          disabled={busy || retrying || sharedRemember.working || sharedInspirations.working || !sessionReady}
          onClick={() => recoverSharedTask('dismiss')}>结束原请求</button>}
      </div>}
      {isShared && sharedTask.visible?.error && <div className="workbench-error" role="alert">
        <span>{sharedTask.visible.error}</span><button type="button" onClick={sharedTask.refresh}>核对任务记录</button>
      </div>}
      {isShared && sharedRemember.visible && sharedRemember.visible.status !== 'idle' && <div className="workbench-error" role="status">
        <span>{{ checking: '核对记住记录', accepted: '原记住已受理', preparing: '正在准备', failed: '记住未受理',
          not_received: '原请求未受理', unknown: '记住结果待核对', storage_error: '记住记录未读取' }[sharedRemember.visible.status]}</span>
        {sharedRemember.visible.error && <span>{sharedRemember.visible.error}</span>}
        {sharedRemember.visible.status === 'accepted'
          ? <button type="button" disabled={busy || retrying} onClick={() => {
            if (!sharedOperationPending()) adoptSharedRemember(sharedRemember.visible, rememberAdoption());
          }}>查看回执</button>
          : <button type="button" disabled={sharedRemember.working} onClick={sharedRemember.check}>核对</button>}
        {projectScope?.can_write && sharedRemember.visible.canResume
          && <button type="button" disabled={sharedRemember.working || sharedInspirations.working || busy || retrying}
            onClick={async () => {
              if (retrying || busy || sharedOperationPending()) return;
              const origin = rememberAdoption();
              adoptSharedRemember(await sharedRemember.retry(), origin);
            }}>继续原请求</button>}
        {sharedRemember.visible.status === 'failed' && !sharedRemember.visible.canResume && <button type="button"
          onClick={() => sharedRemember.settle(sharedRemember.visible.command)}>关闭</button>}
      </div>}
      {!isShared && [...visible.turns.filter(turn => turn.id !== currentStream?.id), ...(currentStream ? [currentStream] : [])].map(turn => <Interventions key={`${userStorageKey('steer')}:${projectId}:${turn.id}`} projectId={currentStream?.id === turn.id ? streamProject : projectId} threadId={currentStream?.id === turn.id ? currentStream.threadId || visible.id : visible.id} turn={turn} active={currentStream?.id === turn.id || busyReceipt(turn.receipt)} onNewRound={(text, accept) => setInterventionDraft({ scope, text, accept: () => { accept(); setInterventionDraft(null); } })}/>)}
      <Composer key={`${projectId}:${composerRevision}`} fixedIntent={sharedIntents?.length === 1 ? sharedIntents[0] : undefined}
        allowedIntents={sharedIntents}
        allowFiles={!isShared} interventionDraft={interventionDraft?.scope === scope ? interventionDraft : null} resetContinuation={continuationReset} draft={draft} onDraft={onDraft} handoff={isShared ? undefined : handoff} onHandoff={onHandoff} projects={isShared ? [] : projects} onExecutor={allowExternalExecutor && continuation?.scope !== scope ? openExecutor : undefined} executorIntent={allowExternalExecutor && continuation?.scope !== scope && externalExecutor.value !== 'builtin' ? 'do' : null} onAutomatic={externalExecutor.reset} onRemind={isShared ? undefined : () => onNavigate?.('companion', { tab: 'schedule' })}
        projectTag={isShared ? projectScope?.name || projectId : projects.find(project => project.id === composerProject)?.name || ({ default: '日常', inbox: '收件箱', me: '我' })[composerProject] || '项目'} onSend={send}
        tools={allowExternalExecutor ? ({ intent, files }) => <ExternalExecutorButton value={continuation?.scope !== scope && !files.length && (!intent || intent === 'do') ? externalExecutor.value : 'builtin'} onOpen={openExecutor} unavailable={continuation?.scope !== scope && !files.length && (!intent || intent === 'do') && externalExecutor.unavailable} disabled={busy || !sessionReady || continuation?.scope === scope || !!files.length || (!!intent && intent !== 'do')}/> : undefined}
        prefill={continuation?.scope === scope ? continuation : proposedInput || (example?.scope === scope ? example : null)} onPrefill={id => { if (id === inputProposal?.id) onInputProposalHandled?.(); }} onRecord={isShared ? undefined : record} recording={recording} sendDisabled={busy || isShared && (!projectScope || retrying || sharedInspirations.working || sharedRemember.working)} disabled={!sessionReady || isShared && !sharedInspirations.ready}/>
    </div>
    {empty && sessionReady && !isShared && <WorkbenchHome projectId={projectId} projects={projects} threads={visible.threads} onNavigate={onNavigate} onProjectCreated={onProjectCreated} hasDraft={Boolean(draft?.text)} onSample={value => setExample({ ...value, scope, id: `example-${++continuationSequence.current}` })}/>}
  </main>{allowExternalExecutor && continuation?.scope !== scope && externalExecutor.open && <ExternalExecutorPicker {...externalExecutor} onSettings={() => onNavigate?.('settings', { section: 'connect' })}/>}
  {sharePanel?.scope === scope && (!isShared || serverConversationShares) && (sharePanel.remote && sharePanel.source && serverConversationShares
    ? <RemoteConversationShare source={sharePanel.source} onClose={closeShare} onNavigate={onNavigate} onLocal={!isShared
      ? () => setSharePanel(current => current?.scope === scope ? { ...current, remote: false } : current) : undefined}/>
    : <ConversationSharePreview scope={scope} snapshot={sharePanel.snapshot} loading={sharePanel.loading} error={sharePanel.error}
      onClose={closeShare} onRetry={() => openShare(sharePanel.turn, sharePanel.targetProject)}
      extra={serverConversationShares && sharePanel.source && <button type="button" className="ui-filter"
        onClick={() => setSharePanel(current => current?.scope === scope ? { ...current, remote: true } : current)}>发给成员</button>}/>)}
  {draftPanel?.scope === scope && tracePanel?.scope !== scope && (draftPanel.documentId
    ? <WorkbenchDraftPanel key={`${draftPanel.projectId || projectId}:${draftPanel.documentId}`} projectId={draftPanel.projectId || projectId} documentId={draftPanel.documentId} itemId={draftPanel.itemId} navigation={panelNavigation('draft')} onClose={() => setDraftPanel(null)}/>
    : <WorkbenchOriginalPanel key={`${draftPanel.projectId}:${draftPanel.itemId}`} projectId={draftPanel.projectId} itemId={draftPanel.itemId} onClose={() => setDraftPanel(null)}/>)}
    {outputPanel?.scope === scope && <WorkbenchOutcomePanel projectId={outputPanel.project || projectId} documentId={outputPanel.documentId} document={outputPanel.document} loading={outputPanel.loading} error={outputPanel.error} task={outputPanel.task} qualified={outputPanel.qualified} previous={outputPanel.previous} onRetryPrevious={reloadOutputPrevious} onContinue={continueOutput} onUpdate={updateOutcome} updateDisabled={busy || retrying || Boolean(reviewing)} hasPendingUpdate={Boolean(readStoredJson(userStorageKey(`outcome-update:${outputPanel.project || projectId}:${outputPanel.documentId}`)).value)} onSourcesChanged={refreshPlacement} onRestore={task => openOutput(task, outputPanel.project || projectId)} navigation={panelNavigation('outcome')} onClose={closeOutput}/>}
    {tracePanel?.scope === scope && citationPanel?.scope !== scope && <ContextPanel kind={tracePanel.kind || "ask"} receipt={tracePanel.ask} onClose={() => setTracePanel(null)} onOpenCitation={citation => openCitation({ ...citation, project: citation.project || tracePanel.projectId || projectId }, 'context')} navigation={panelNavigation('context')} onSettings={section => openSettings(section, tracePanel.projectId || projectId)} projectName={projects.find(project => project.id === (tracePanel.projectId || projectId))?.name || tracePanel.projectId || projectId}/>}
    {citationPanel?.scope === scope && <ContextCitation panel={citationPanel} onClose={closeCitation} onRetry={() => openCitation(citationPanel.citation, citationPanel.origin)}/>}
    {choicePanel?.scope === scope && <FocusPanel title="成果" onClose={() => { gate.invalidate('outcome-choices'); setChoicePanel(null); }}>{choicePanel.loading ? <StatusDot state="processing"/> : choicePanel.error ? <button type="button" onClick={() => chooseOutput(choicePanel.turn)}>读取未完成 · 重试</button> : (choicePanel.items ?? []).map(row => <Row key={row.document_id} title={row.title} meta={`v${row.version}`} onOpen={() => redoOutput(choicePanel.turn, row.document_id)}/>)}</FocusPanel>}
    {correctionPanel?.scope === scope && <FocusPanel title="纠正分段" onClose={() => setCorrectionPanel(null)}>
      <RouteCorrectionPanel turnId={correctionPanel.turnId} projectId={correctionPanel.projectId} initialIntent={correctionPanel.initialIntent} onCompleted={correctionCompleted}
        onOpenReplacement={proof => { setCorrectionPanel(null); correctionCompleted(proof); }}/>
    </FocusPanel>}
  </div>;
}

function ContextCitation({ panel, onClose, onRetry }) {
  const labels = { insight: '认识', summary: '整理稿', note: '整理稿', source: '原件', inspiration: '灵感' };
  const entry = panel.layer === 'source' ? panel.original : panel.data?.[panel.layer === 'summary' ? 'note' : panel.layer];
  const snippet = panel.data?.source?.window;
  return <FocusPanel title={labels[panel.layer]} onClose={onClose}>
    <section id="workbench-citation">
      {panel.loading ? <StatusDot state="processing"/> : panel.layer === 'source' && !entry ? <>
        <p role="alert">全文未读取 <button type="button" onClick={onRetry}>重试</button></p>
        {panel.citation.quote && <blockquote className="workbench-citation-quote">{panel.citation.quote}</blockquote>}
        {snippet && <p>{snippet.pre}{snippet.quote}{snippet.post}</p>}
      </> : panel.error || !entry ? <p role="alert">读取未完成 <button type="button" onClick={onRetry}>重试</button></p> : <>
        {entry.title && <h3>{entry.title}</h3>}
        {panel.layer === 'insight' ? <p>{entry.text}</p> : <CitationBody text={['source', 'inspiration'].includes(panel.layer) ? entry.text : entry.markdown} coordinateSpace={['source', 'inspiration'].includes(panel.layer) ? entry.coordinate_space : 'document_markdown_v1'} citation={panel.citation} projectId={panel.project} sourceId={panel.layer === 'source' ? panel.citation.id : undefined} scopeKey={panel.scope}/>}
      </>}
    </section>
  </FocusPanel>;
}
export default Workbench;

function OutcomeReceiptChoices({ turn, onNew, onOther }) {
  return <details><summary aria-label="成果选择"><Icon name="more"/></summary>
    <button type="button" className="ui-filter" onClick={onNew}>新写一篇</button>
    <button type="button" className="ui-filter" onClick={onOther}>换一篇</button>
  </details>;
}
