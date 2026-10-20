import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Composer, Receipt, InsightChip, ProgressDots, StatusDot, CitedAnswer, LayerBadges, FocusPanel, ConversationSharePreview, MarkdownBody, Icon } from '../../shared/ui';
import { readStoredJson, writeStoredJson } from '../../shared/lib/browserStorage';
import { userStorageKey } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { answerWithoutCitationMarks } from '../../shared/ui/CitedAnswer';
import { createConversationSnapshot } from '../../shared/lib/conversationSnapshot';
import { displayScopedText, formatScopeTag, scopedText } from '../../shared/lib/scopeTag';
import { sendSignal, signalEpoch } from '../../shared/signalsApi';
import { recognitionApi } from '../../shared/api/recognitionApi';
import { remoteRestriction } from '../../shared/lib/privacyMessages';
import { workbenchApi, workbenchErrorMessage } from './workbenchApi';
import { libraryApi } from '../library/libraryApi';
import { ContextPanel } from './ContextPanel';
import { CitationBody } from './CitationBody';
import { WorkbenchDraftPanel } from './WorkbenchDraftPanel';
import { WorkbenchOriginalPanel } from './WorkbenchOriginalPanel';
import { WorkbenchOutcomePanel } from './WorkbenchOutcomePanel';
import { TaskDivision } from './TaskDivision';
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
const settledStop = turn => stoppedTurn(turn) || ['done', 'partial', 'failed'].includes(turn?.receipt?.do?.state)
  || Boolean(turn?.receipt?.ask?.error)
  || typeof turn?.receipt?.ask?.answer === 'string' && !turn.receipt.ask.interruption;
function StopButton({ disabled, onClick }) {
  return <button type="button" className="workbench-stop" aria-label="停止" title="停止" disabled={disabled} onClick={onClick}>
    <Icon name="review-stop" size={14}/>
  </button>;
}
function WorkbenchError({ error, fallback, projectId, onSettings, onRetry, retryDisabled = false }) {
  if (!error) return null;
  const restriction = remoteRestriction(error);
  const message = onRetry ? '读取未完成' : fallback && typeof error === 'string'
    && !['interrupted', 'storage_quota_exceeded', 'job_quota_exceeded'].includes(error)
    ? fallback : workbenchErrorMessage(error, fallback);
  return <div role="alert" className="workbench-error" title={restriction?.title}>
    {restriction ? <><span>{restriction.reason}</span><span aria-hidden="true"> · </span><button type="button" disabled={!onSettings}
      onClick={() => onSettings?.(restriction.section, projectId)}>查看设置</button></> : message}
    {onRetry && <><span aria-hidden="true"> · </span><button type="button" disabled={retryDisabled} onClick={onRetry}>重试</button></>}
  </div>;
}
const WorkbenchTurn = memo(function WorkbenchTurn({ turn, turnId, projectId, errorProjectId = projectId, shareQuestion = turn.user_text, scope, reviewing, continuing, streamText, streamStatus, actions }) {
  const { review, retry, continueTurn, openOutput, openCitation, openShare, redoOutput, chooseOutput, closeOutput, closeCitation,
    setTracePanel, setDraftPanel, setSession, onNavigate, openSettings, askElsewhere, redoAs, projects, sending, refreshPlacement, setError, stopTurn, stops } = actions;
  const [copied, setCopied] = useState(false), copyTimer = useRef(null), currentScope = useRef(scope), alive = useRef(true);
  const [more, setMore] = useState(false), moreButton = useRef(null);
  currentScope.current = scope;
  useEffect(() => { alive.current = true; setCopied(false); return () => { alive.current = false; clearTimeout(copyTimer.current); }; }, [scope]);
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
      const redoItems = turn.user_text && turn.intent !== 'inspiration' ? ['remember', 'ask', 'do'].filter(key => key !== turn.intent)
        .map(key => <button key={key} type="button" role="menuitem" disabled={sending} onClick={() => { setMore(false); moreButton.current?.focus(); redoAs(turn, key); }}>{REDO_LABELS[key]}</button>) : [];
      const moreMenu = items => <div className="ui-conversation-menu" onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setMore(false); }} onKeyDown={event => {
        if (event.key === 'Escape') { event.stopPropagation(); setMore(false); moreButton.current?.focus(); }
      }}><button ref={moreButton} type="button" aria-label="更多" title="更多" aria-haspopup="menu" aria-expanded={more} onClick={() => setMore(value => !value)}><Icon name="more" size={16}/></button>
        {more && <div role="menu" aria-label="对话动作">{items}</div>}
      </div>;
      if (turn.intent === 'multi') {
        const parts = turn.receipt.parts || [];
        return <article key={turn.id} id={`workbench-${turn.id}`} tabIndex={-1} className={turn.id === turnId ? 'is-target' : ''}>{turn.user_text && <div className="workbench-bubble">{displayScopedText(turn.user_text)}</div>}
          <div className="workbench-parts" aria-label="拆分">{parts.map(part => <span key={part.index} title={part.span}><Icon name={part.intent} size={12}/>{part.span.length > 12 ? part.span.slice(0, 12) + '…' : part.span}</span>)}{!!redoItems.length && moreMenu(redoItems)}</div>
          {parts.map(part => part.turn_id && part.receipt && Object.keys(part.receipt).length
            ? <WorkbenchTurn key={part.index} turn={{ ...turn, id: part.turn_id, intent: part.intent, user_text: null, receipt: part.receipt }} shareQuestion={part.span} turnId={turnId} projectId={part.project_id || projectId} errorProjectId={part.project_id || errorProjectId} scope={scope} reviewing={reviewing} continuing={continuing} actions={actions}/>
            : <Receipt key={part.index} kind={part.intent}><StatusDot state={['failed', 'not_started'].includes(part.state) ? 'failed' : part.state === 'running' ? 'processing' : 'pending'}/>{remoteRestriction(part.error) && <WorkbenchError error={part.error} projectId={part.project_id || errorProjectId} onSettings={openSettings}/>}</Receipt>)}
        </article>;
      }
      const memory = turn.receipt.remember, inspiration = turn.receipt.inspiration, ask = turn.receipt.ask, task = turn.receipt.do;
      const externalTask = Boolean(task?.executor && task.executor !== 'builtin');
      const externalTaskError = typeof task?.error === 'string' ? task.error : task?.error?.code;
      const interruption = (ask || task)?.interruption;
      const interrupted = !ask?.error && ['connection', 'sleep', 'stopped'].includes(interruption);
      const stopping = Boolean(stops[`${projectId}:${turn.id}`]);
      const canContinue = Boolean(ask) && !ask.error || !externalTask && ['interrupted', 'stopped'].includes(task?.state);
      const stopControl = !externalTask && (task && ['researching', 'preparing', 'running', 'waiting_approval'].includes(task.state)
        || ask && !ask.error && !ask.interruption && ask.answer === null)
        ? <StopButton disabled={stopping} onClick={() => stopTurn(turn, projectId)}/> : null;
      const hint = ask?.elsewhere, hintName = hint && (projects.find(project => project.id === hint.project_id)?.name || hint.project_id);
      const hintScene = hint?.scene;
      const hintLabel = hint && `#${hintName}${hintScene ? '/' + hintScene : ''}`;
      const shareable = typeof ask?.answer === 'string' || typeof inspiration?.insight?.text === 'string'
        || Boolean(memory?.state === 'done' && memory.document_id)
        || Boolean(['done', 'partial'].includes(task?.state) && task.document_id);
      const shareMenu = moreMenu(<>{memory?.item_id && !memory.document_id && <button type="button" role="menuitem" aria-label="打开原件" onClick={() => { setMore(false); moreButton.current?.focus(); closeOutput(); closeCitation(); setTracePanel(null); setDraftPanel({ scope, projectId, itemId: memory.item_id }); }}><Icon name="arrow-up-right" size={14}/>原件</button>}<button type="button" role="menuitem" disabled={!shareable} title={shareable ? undefined : '完成后分享'} onClick={() => { setMore(false); moreButton.current?.focus(); openShare({ ...turn, user_text: shareQuestion }); }}>分享</button>{redoItems}</>);
      return <article key={turn.id} id={`workbench-${turn.id}`} tabIndex={-1} className={turn.id === turnId ? 'is-target' : ''}>
        {turn.user_text && <div className="workbench-bubble">{displayScopedText(turn.user_text)}</div>}
        <Receipt kind={turn.intent}>{memory ? <>
          <div className="workbench-receipt-title"><StatusDot state={memory.state}/><span className="workbench-memory-title" title={memory.title}>{memory.title}</span><ProgressDots {...memory.progress} running={memory.state === 'processing'} title="原件 · 正文 · 整理 · 入库"/>{memory.document_id && <button type="button" aria-label="打开整理稿" onClick={() => { closeOutput(); closeCitation(); setTracePanel(null); setDraftPanel({ scope, documentId: memory.document_id, itemId: memory.item_id }); }}>↗</button>}{memory.state === 'failed' && <button type="button" disabled={sending || continuing} onClick={() => retry(turn, projectId)}>重试</button>}</div>
          <DocumentPlacement projectId={projectId} projects={projects} row={{...memory,revision:memory.document_revision}} onChanged={refreshPlacement} onError={setError}/><div className="workbench-insights">{memory.insights.map(insight => chip(insight, memory.filing?.state === 'filed' ? memory.filing.target_project_id : undefined))}{!!memory.related.length && <span className="workbench-related-count" title={memory.related.map(entry => entry.text).join('\n')}>∞ {memory.related.length}</span>}</div><WorkbenchError error={memory.error} fallback="整理未完成 · 重试" projectId={errorProjectId} onSettings={openSettings}/>
        </> : ask ? <>
          {stopControl && <div className="workbench-receipt-title">{stopControl}</div>}
          <CitedAnswer answer={ask.answer ?? ask.partial ?? ''} citations={ask.citations} onOpenCitation={openCitation} citationHref="#workbench-citation" onCopyAnswer={() => recordCopy()}/><div className="workbench-ask-layers"><LayerBadges layers={ask.layers} onOpenTrace={() => { closeOutput(); closeCitation(); setTracePanel({ scope, ask }); }}/>{hint && <span className="ui-layer-badges workbench-elsewhere"><button type="button" title={`在 ${hintLabel} 里问`} aria-label={`在 ${hintLabel} 里问`} disabled={sending} onClick={() => askElsewhere(turn, hint)}>→ {hintLabel}</button></span>}<button type="button" className="workbench-answer-copy" aria-label="复制回答" title="复制" onClick={copyAnswer}><Icon name={copied ? 'check' : 'copy'} size={14}/></button></div>
        </> : task ? <>
          <div className="workbench-receipt-title"><StatusDot state={['researching', 'preparing', 'running'].includes(task.state) ? 'processing' : task.state === 'waiting_approval' ? 'pending' : task.state}/><span className="workbench-memory-title" title={task.title}>{task.title}</span><button type="button" className="workbench-context-open" aria-label="本次上下文" onClick={() => { closeOutput(); closeCitation(); setTracePanel({ scope, ask: task, kind: 'do' }); }}>◯ {contextPercent(task.context)}</button><ProgressDots {...task.progress} running={['researching', 'preparing', 'running'].includes(task.state)} title={task.kernel_turn_id ? '拆活 · 干活 · 汇总' : task.research_turn_id ? '研究 · 准备 · 批准 · 成果' : '准备 · 批准 · 成果'}/><OutcomeVersion version={task.continues?.version}/>{task.executor && task.executor !== 'builtin' && EXECUTOR_LABELS[task.executor] && <span className="workbench-executor-source">来自 {EXECUTOR_LABELS[task.executor]}</span>}{task.fallback_new && <span className="ui-count" aria-label="已新写一篇">新写一篇</span>}{!externalTask && ['done','partial'].includes(task.state) && task.document_id && <OutcomeReceiptChoices turn={turn} onNew={() => redoOutput(turn, null)} onOther={() => chooseOutput(turn)}/>}{['done','partial'].includes(task.state) && task.document_id && <button type="button" aria-label="打开成果" onClick={() => openOutput(task)}>↗</button>}{stopControl}</div>
          {!externalTask && task.kernel_turn_id && <TaskDivision key={`${projectId}:${turn.id}`} project={projectId} turnId={turn.id} task={task} onReference={reference => onNavigate?.('workbench', reference)} onRedo={result => setSession(current => current.project === projectId ? { ...current, id: result.thread_id, turns: [...current.turns, result.turn] } : current)}/>}{!task.kernel_turn_id && !!task.experts?.length && <div className="workbench-experts">{task.experts.map((expert, index) => <span key={index} role="img" aria-label={expert.role} title={expert.role}><StatusDot aria-hidden="true" state={expert.state === 'running' ? 'processing' : expert.state}/></span>)}</div>}
        </> : inspiration?.insight ? <>{chip(inspiration.insight, projectId)}{inspiration.placement && projects.some(project=>project.id===inspiration.placement.project_id) && <InboxFiling key={`${scope}:${inspiration.insight.id}:${inspiration.insight.revision}`} row={inspiration.insight} targetProjectId={inspiration.placement.project_id} scenes={projects.find(project=>project.id===inspiration.placement.project_id)?.scenes || []} suggestion={inspiration.placement.scene} onFiled={refreshPlacement} onError={setError}/>}</> : null}
          {!memory && (ask?.error || externalTask && task.error || remoteRestriction((ask || task)?.error)) && <WorkbenchError error={externalTask ? externalTaskError || 'external_task_execution_failed' : (ask || task).error} fallback={externalTask ? task.state === 'interrupted' || externalTaskError === 'external_task_interrupted' ? '外部执行已中断' : '外部执行未完成' : undefined} projectId={errorProjectId} onSettings={openSettings}/>}
          {task && interrupted && <>{interruption !== 'stopped' && <RetryCountdown status={streamStatus}/>}<CitedAnswer answer={interruption === 'stopped' ? task.partial || '' : streamText ?? task.partial ?? ''}/></>}
          {interrupted && <div className="workbench-interrupted"><span>{interruption === 'stopped' ? '已停止' : interruption === 'sleep' ? '电脑休眠' : '连接中断'}</span>{canContinue && <><span aria-hidden="true">·</span><button type="button" disabled={continuing || stopping} onClick={() => continueTurn(turn, projectId)}>继续</button></>}</div>}
        </Receipt>{shareMenu}
      </article>;
});

export function Workbench({ projectId = 'default', projects = EMPTY_PROJECTS, threadId, turnId, onNavigate, draft, onDraft, handoff, onHandoff, onProjectCreated, onProjectsStale }) {
  const externalExecutor = useExternalExecutor(projectId);
  const gate = useRequestScope(`${projectId}:${threadId || ''}:${turnId || ''}`), scope = gate.scope;
  const [session, setSession] = useState({ scope: threadId || turnId ? null : scope, project: projectId, threads: [], id: null, turns: [] });
  const sessionReady = session.scope === scope && session.project === projectId;
  const visible = sessionReady ? session : { threads: [], id: null, turns: [] };
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
    if (gate.isScopeCurrent(scope) && ['privacy', 'model'].includes(section))
      onNavigate?.('settings', { section, project_id: targetProject });
  }, [gate, scope, projectId, onNavigate]);
  const [retrying, setRetrying] = useState(false);
  const [recording, setRecording] = useState(false);
  const [tracePanel, setTracePanel] = useState(null);
  const [citationPanel, setCitationPanel] = useState(null);
  const [outputPanel, setOutputPanel] = useState(null);
  const [continuation, setContinuation] = useState(null), [choicePanel, setChoicePanel] = useState(null);
  const continuationSequence = useRef(0);
  const outcomeRedoPending = useRef(null);
  const [draftPanel, setDraftPanel] = useState(null);
  const [sharePanel, setSharePanel] = useState(null);
  const [streaming, setStreaming] = useState(null);
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
  const cacheKey = userStorageKey(`chriptmas-v2-thread:${projectId}`);
  function retryThreadRead() {
    if (gate.isScopeCurrent(scope) && threadReadFailure === scope && !busy && !reviewing && !retrying)
      setThreadReadRetry(value => value + 1);
  }
  useEffect(() => () => { streamController.current?.abort(); }, [scope]);
  useEffect(() => () => {
    for (const operation of stopRequests.current.values()) operation.controller.abort();
    stopRequests.current.clear();
  }, [scope]);
  useEffect(() => {
    outcomeRedoPending.current = null;
    setError(''); setBusy(false); setReviewing(null); setRetrying(false); setRecording(false); setTracePanel(null); setCitationPanel(null); setOutputPanel(null); setDraftPanel(null); setSharePanel(null); setStreaming(null); setUnknownProject(null); setContinuation(null); setChoicePanel(null); creatingProject.current = false;
  }, [projectId, threadId, turnId]);
  useEffect(() => {
    const request = gate.issue('thread', scope);
    if (threadReadFailure === scope) setError('');
    setThreadReadFailure(null);
    async function load() {
      try {
        const list = await workbenchApi.threads(projectId);
        if (!gate.isCurrent(request)) return;
        const threads = list.items || [];
        let id = threadId || readStoredJson(cacheKey).value;
        // 空字符串记录用户主动新建的空对话，切页或刷新继续保留。
        if (id !== '' && !threads.some(thread => thread.id === id)) id = threads[0]?.id || null;
        if (id === '') id = null;
        let detail = id ? await workbenchApi.thread(projectId, id) : { turns: [] };
        if (!gate.isCurrent(request)) return;
        if (turnId && !detail.turns.some(turn => turn.id === turnId)) {
          for (const thread of threads.filter(thread => thread.id !== id)) {
            const candidate = await workbenchApi.thread(projectId, thread.id);
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
          signal: controller.signal,
          onDelta: delta => { received += delta; text(received); },
          onReset: value => { if (typeof value.text === 'string') { received = value.text; text(received); } },
          onStatus: value => { if (gate.isCurrent(request)) setStreaming(current => current?.scope === scope
            ? { ...current, status: value.state === 'retrying' ? value : null } : current); },
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
    if ((!processing && !awaiting) || (!visible.id && !awaiting) || (!awaiting && (busy || reviewing || retrying))) return;
    const id = awaiting?.threadId || visible.id, project = awaiting?.project || projectId;
    if (!id) return;
    const controller = new AbortController();
    let timer, cancelled = false;
    async function poll() {
      const request = gate.issue('poll', scope);
      try {
        const detail = await workbenchApi.thread(project, id, { signal: controller.signal });
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
  }, [projectId, threadId, turnId, visible.id, processing, busy, reviewing, retrying, stops, pendingContinue]);
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
  async function send({ text, files, intent, onAccepted }, createdProject) {
    if ((busy && !createdProject) || !sessionReady) return false;
    const request = gate.issue('send', scope);
    gate.invalidate('thread'); gate.invalidate('poll');
    setBusy(true); setError(''); setUnknownProject(null);
    const controller = new AbortController(); streamController.current = controller;
    let attemptedProject = projectId, acceptedTurn = null, acceptedText = false;
    const acceptedFiles = [];
    const acceptance = () => acceptedFiles.length ? (acceptedFiles.length === files.length ? true : { acceptedFiles }) : acceptedText;
    const originalThread = visible.id, originalIdentity = userStorageKey('workbench-send');
    try {
      const executor = continuation?.scope !== scope && !files.length && (!intent || intent === 'do') ? await externalExecutor.validate(controller.signal) : 'builtin';
      if (!gate.isCurrent(request)) return false;
      if (executor !== 'builtin') intent = 'do';
      const project = createdProject || targetProject(text, intent, projectId, projects);
      attemptedProject = project;
      const threadCacheKey = userStorageKey(`chriptmas-v2-thread:${project}`);
      let id = project === projectId ? visible.id : null;
      const retry = attachmentRetry.current;
      if (retry?.scope === scope && retry.project === project && files.length && files.every(file => retry.files.includes(file))) id = retry.threadId;
      const images = files.length > 1 && files.every(file => /\.(png|jpe?g|bmp|tiff?|webp)$/i.test(file.name));
      const batches = images ? [files] : files.map(file => [file]);
      if (!batches.length) batches.push([]);
      for (const batch of batches) {
        const [file, ...others] = batch;
        let uploaded = file ? attachmentUploads.current.find(value => value.identity === originalIdentity && value.project === project
          && value.files.length === batch.length && value.files.every((entry, index) => entry === batch[index])) : null;
        if (file && !uploaded) {
          uploaded = { identity: originalIdentity, project, files: batch, item: await workbenchApi.file(project, file, others) };
          attachmentUploads.current.push(uploaded);
        }
        const item = uploaded?.item;
        if (!gate.isCurrent(request)) return acceptance();
        let batchAccepted = false;
        const acceptBatch = () => { if (!batchAccepted) {
          acceptedFiles.push(...batch); batchAccepted = true;
          attachmentUploads.current = attachmentUploads.current.filter(value => value !== uploaded);
        } };
        const result = await workbenchApi.create({ project_id: project, ...(id ? { thread_id: id } : {}), text, ...(file ? { intent: 'remember' } : intent ? { intent } : {}), ...(item ? { item_id: item.id } : {}),
          ...(executor !== 'builtin' ? { executor } : {}),
          ...(executor === 'builtin' && !file && intent === 'do' && project === projectId && continuation?.scope === scope ? { continue_from: continuation.documentId } : {}) }, {
          signal: controller.signal,
          onStarted: value => { acceptBatch(); if (gate.isCurrent(request)) {
            setStreaming({ scope, ...value.turn, project, executor, threadId: value.thread_id, answer: '' });
            const acceptedThread = typeof value.thread_id === 'string' && value.thread_id ? value.thread_id : id;
            if (file && acceptedThread) attachmentRetry.current = { scope, project, threadId: acceptedThread, files: files.filter(value => !acceptedFiles.includes(value)) };
            if (value.turn.intent === 'do' && acceptedThread && (project !== projectId || !originalThread || acceptedThread === originalThread))
              acceptedTurn = { project, id: acceptedThread, turnId: value.turn.id };
            if (typeof value.thread_id === 'string') writeStoredJson(threadCacheKey, value.thread_id);
          } },
          onDelta: delta => { if (gate.isCurrent(request)) setStreaming(current => current?.scope === scope ? { ...current, answer: current.answer + delta, status: null } : current); },
          onReset: value => { if (gate.isCurrent(request) && typeof value.text === 'string') setStreaming(current => current?.scope === scope ? { ...current, answer: value.text, status: null } : current); },
          onStatus: value => { if (gate.isCurrent(request)) setStreaming(current => current?.scope === scope ? { ...current, status: value.state === 'retrying' ? value : null } : current); },
        });
        acceptBatch();
        if (!gate.isCurrent(request)) return acceptance();
        id = result.thread_id;
        if (file) attachmentRetry.current = { scope, project, threadId: id, files: files.filter(value => !acceptedFiles.includes(value)) };
        writeStoredJson(threadCacheKey, id);
        const favorites = !file && (!intent || intent === 'remember') && /https:\/\/(?:space\.bilibili\.com\/\d+\/favlist|(?:www\.)?bilibili\.com\/medialist\/detail\/ml\d+)/.test(text);
        const expanded = favorites ? await workbenchApi.thread(project, id) : null;
        if (!gate.isCurrent(request)) return acceptance();
        if (project === 'default' && result.turn?.id) sentHere.current.add(result.turn.id);
        if (project === projectId) setSession(current => ({ scope, project, id, threads: current.threads.some(thread => thread.id === id) ? current.threads : [{ id, title: displayScopedText(result.turn.user_text || file?.name || '') }, ...current.threads], turns: expanded?.turns || (current.id === id ? current.turns.some(old => old.id === result.turn.id) ? replaceReceiptTurn(current.turns, result.turn) : [...current.turns, result.turn] : [result.turn]) }));
      }
      setContinuation(null);
      attachmentRetry.current = null;
      externalExecutor.reset();
      if (project !== projectId || id !== visible.id) onNavigate?.('workbench', { project_id: project, thread_id: id });
      return true;
    } catch (failure) { if (gate.isCurrent(request)) {
      clearFinishedStop(failure);
      setError(failure, attemptedProject);
      if (failure.projectTag) setUnknownProject({ scope, name: failure.projectTag, input: { text, files, intent, onAccepted } });
      // 错误帧可能晚于失败回执落盘；只回读本次已接受的线程，不重发任务。
      if (acceptedTurn && !controller.signal.aborted && userStorageKey('workbench-send') === originalIdentity) {
        try {
          const detail = await workbenchApi.thread(acceptedTurn.project, acceptedTurn.id);
          if (gate.isCurrent(request) && userStorageKey('workbench-send') === originalIdentity) {
            if (!detail.turns?.some(turn => turn.id === acceptedTurn.turnId)) return acceptance();
            failure.clearAcceptedRequest?.();
            acceptedText = true;
            writeStoredJson(userStorageKey(`chriptmas-v2-thread:${acceptedTurn.project}`), acceptedTurn.id);
            if (acceptedTurn.project === projectId) {
              if (projectId === 'default') sentHere.current.add(acceptedTurn.turnId);
              setSession(current => current.scope === scope && current.project === projectId
                && (current.id === originalThread || current.id === acceptedTurn.id)
                ? { ...current, id: acceptedTurn.id, turns: detail.turns,
                  threads: current.threads.some(thread => thread.id === acceptedTurn.id) ? current.threads
                    : [{ id: acceptedTurn.id, title: displayScopedText(text) }, ...current.threads] } : current);
            } else {
              onAccepted?.(acceptance());
              onNavigate?.('workbench', { project_id: acceptedTurn.project, thread_id: acceptedTurn.id });
            }
          }
        } catch { /* 保留原任务错误和输入，回读失败仍可由用户刷新查看。 */ }
      }
    } return acceptance(); }
    finally { if (streamController.current === controller) streamController.current = null; if (gate.isCurrent(request)) { setBusy(false); setStreaming(null); } }
  }
  const askElsewhere = useCallback((turn, hint) => {
    if (!gate.isScopeCurrent(scope)) return;
    return send({ text: `${formatScopeTag(hint.project_id, hint.scene || null)}\n${scopedText(turn.user_text).text}`, files: [], intent: 'ask' });
  }, [busy, sessionReady, gate, scope, projectId, projects, visible.id, onNavigate]);
  // 在日常里刚问的问题没找到或答得不全、资料在别的项目时，自动去那个项目再问一次（资料会被自动归走）。
  // 只跟进本页刚发出的轮次；拆分出的问题在后台完成，所以随轮询结果检查。
  const sentHere = useRef(new Set());
  useEffect(() => {
    if (projectId !== 'default' || busy || !sessionReady) return;
    for (const turn of visible.turns) {
      if (!sentHere.current.has(turn.id)) continue;
      const asked = turn.intent === 'multi' ? (turn.receipt?.parts || []).filter(part => part.intent === 'ask').map(part => ({ user_text: part.span, ask: part.receipt?.ask }))
        : turn.intent === 'ask' ? [{ user_text: turn.user_text, ask: turn.receipt?.ask }] : [];
      const lost = asked.find(row => row.ask?.elsewhere?.project_id);
      if (lost) { sentHere.current.delete(turn.id); askElsewhere({ user_text: lost.user_text }, lost.ask.elsewhere); return; }
    }
  }, [visible.turns, projectId, busy, sessionReady, askElsewhere]);
  const redoAs = useCallback((turn, intent) => {
    if (!gate.isScopeCurrent(scope)) return;
    return send({ text: turn.user_text, files: [], intent });
  }, [busy, sessionReady, gate, scope, projectId, projects, visible.id, onNavigate]);
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
      const detail = await workbenchApi.thread(projectId, visible.id);
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
    if (retrying) return;
    const request = gate.issue('retry', scope);
    gate.invalidate('thread'); gate.invalidate('poll'); setRetrying(true);
    try { const updated = await workbenchApi.retry(project, turn.id); if (gate.isCurrent(request)) setSession(current => ({ ...current, turns: replaceReceiptTurn(current.turns, updated) })); }
    catch (failure) { if (gate.isCurrent(request)) setError(failure); }
    finally { if (gate.isCurrent(request)) setRetrying(false); }
  }, [retrying, gate, scope, projectId]);
  const stopTurn = useCallback(async (turn, project = projectId) => {
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
  }, [gate, scope, projectId, visible.id, stops, setError]);
  const continueTurn = useCallback(async (turn, project = projectId) => {
    if (busy || retrying || reviewing) return;
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
  }, [busy, retrying, reviewing, gate, scope, projectId, visible.id, clearFinishedStop]);
  const closeCitation = useCallback(() => { gate.invalidate('citation'); setCitationPanel(null); }, [gate]);
  const closeOutput = useCallback(() => { gate.invalidate('output'); setOutputPanel(null); }, [gate]);
  const closeChoices = useCallback(() => { gate.invalidate('outcome-choices'); setChoicePanel(null); }, [gate]);
  const openOutput = useCallback(async (task) => {
    gate.invalidate('share'); setSharePanel(null);
    setDraftPanel(null); setTracePanel(null); closeCitation(); closeChoices();
    const request = gate.issue('output', scope);
    setOutputPanel({ scope, request, documentId: task.document_id, loading: true, qualified: false });
    // 正文独立打开；只有当前成果链才授予接着写，迟到的资格不能进入新范围。
    libraryApi.outcomeVersions(projectId, task.document_id).then(result => {
      const qualified = Array.isArray(result?.items) && result.items.some(row => row.document_id === task.document_id && Number.isSafeInteger(row.version) && row.version > 0);
      if (gate.isCurrent(request)) setOutputPanel(current => current?.scope === scope && current.documentId === task.document_id ? { ...current, qualified, previous: result.previous } : current);
    }).catch(() => {});
    try {
      const document = await recognitionApi.loadDocument({ documentId: task.document_id, projectId });
      if (gate.isCurrent(request)) {
        setOutputPanel(current => ({ ...current, scope, documentId: task.document_id, document, task, loading: false }));
        libraryApi.opened(projectId, 'document', task.document_id);
      }
    } catch { if (gate.isCurrent(request)) setOutputPanel({ scope, error: true }); }
  }, [gate, scope, projectId, closeCitation, closeChoices]);
  const openCitation = useCallback(async (citation, origin = 'answer') => {
    gate.invalidate('share'); setSharePanel(null);
    setDraftPanel(null);
    if (!['insight', 'summary', 'note', 'source', 'inspiration'].includes(citation.layer) || !citation.id) return;
    closeOutput(); closeChoices();
    if (origin !== 'context') setTracePanel(null);
    const request = gate.issue('citation', scope), project = citation.persona ? 'me' : citation.project || projectId, signalToken = signalEpoch();
    setCitationPanel({ scope, layer: citation.layer, citation, origin, loading: true });
    if (citation.layer === 'source') {
      const [drill, full] = await Promise.allSettled([
        libraryApi.drill(project, 'source', citation.id), libraryApi.sourceText(project, citation.id),
      ]);
      if (!gate.isCurrent(request)) return;
      const original = full.status === 'fulfilled' && typeof full.value.text === 'string' ? full.value : null;
      setCitationPanel({ scope, layer: citation.layer, citation, origin, data: drill.status === 'fulfilled' ? drill.value : null, original, error: !original });
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
    if (!gate.isCurrent(token) || outputPanel?.scope !== scope || !documentId) throw new Error('previous_scope_changed');
    const result = await libraryApi.outcomeVersions(projectId, documentId);
    if (!gate.isCurrent(token)) throw new Error('previous_scope_changed');
    const qualified = Array.isArray(result?.items) && result.items.some(row => row.document_id === documentId && Number.isSafeInteger(row.version) && row.version > 0);
    if (!qualified) throw new Error('previous_unavailable');
    setOutputPanel(current => current?.request === token ? { ...current, qualified, previous: result.previous } : current);
    return result.previous;
  }, [gate, scope, projectId, outputPanel]);
  const redoOutput = useCallback(async (turn, selection) => {
    if (outcomeRedoPending.current) return;
    const token = gate.issue('outcome-redo', scope);
    if (!token) return;
    outcomeRedoPending.current = token;
    setRetrying(true); setError('');
    try {
      const division = await workbenchApi.division(projectId, turn.id);
      if (!gate.isCurrent(token)) return;
      const result = await workbenchApi.redo(projectId, turn.id, division.revision, { continue_from: selection });
      if (gate.isCurrent(token)) { gate.invalidate('outcome-choices'); setChoicePanel(null); setSession(current => ({ ...current, id: result.thread_id, turns: [...current.turns, result.turn] })); }
    } catch (failure) { if (gate.isCurrent(token)) setError(failure); }
    finally { if (outcomeRedoPending.current === token) outcomeRedoPending.current = null; if (gate.isCurrent(token)) setRetrying(false); }
  }, [gate, scope, projectId, retrying]);
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
  function continueOutput() {
    if (!gate.isScopeCurrent(scope) || outputPanel?.scope !== scope || !outputPanel.documentId || !outputPanel.qualified) return;
    externalExecutor.reset(); externalExecutor.onClose();
    setContinuation({ scope, id: ++continuationSequence.current, documentId: outputPanel.documentId,
      text: `接着《${outputPanel.document.title || outputPanel.task.title}》写：`, intent: 'do' });
    closeOutput();
  }
  const closeShare = useCallback(() => { gate.invalidate('share'); setSharePanel(null); }, [gate]);
  const openShare = useCallback(async (turn) => {
    closeChoices();
    const request = gate.issue('share', scope); if (!request) return;
    closeOutput(); closeCitation(); setTracePanel(null); setDraftPanel(null);
    setSharePanel({ scope, turn, loading: true });
    try {
      const receipt = turn.receipt;
      let answer, citations = [];
      if (receipt.ask) { answer = receipt.ask.answer; citations = receipt.ask.citations || []; }
      else if (receipt.inspiration?.insight) answer = receipt.inspiration.insight.text;
      else {
        const documentId = receipt.do?.document_id || receipt.remember?.document_id;
        if (!documentId) throw new Error('分享正文未完成');
        const document = await recognitionApi.loadDocument({ documentId, projectId });
        if (!gate.isCurrent(request)) return;
        if (typeof document?.markdown !== 'string' || !Number.isInteger(document.revision) || document.revision < 1
          || (document.document_id || document.id) !== documentId) throw new Error('分享正文未读取');
        answer = document.markdown;
      }
      const snapshot = createConversationSnapshot({ question: displayScopedText(turn.user_text || ''), answer, citations });
      if (gate.isCurrent(request)) setSharePanel({ scope, turn, snapshot });
    } catch { if (gate.isCurrent(request)) setSharePanel({ scope, turn, error: true }); }
  }, [gate, scope, projectId, closeOutput, closeCitation, closeChoices]);
  // 日常 is created by the first remembered item and automatic filing can create
  // a project; reload the list once per settled item so receipts show names, not ids.
  const memories = visible.turns.flatMap(turn => [turn.receipt?.remember, ...(turn.receipt?.parts || []).map(part => part.receipt?.remember)]).filter(Boolean);
  const unnamed = [...new Set([projectId, ...memories.map(memory => memory.filing?.target_project_id)])]
    .filter(id => id && !projects.some(project => project.id === id)).sort().join(',');
  const projectsStale = unnamed && `${unnamed}|${memories.filter(memory => memory.state !== 'processing').length}`;
  const projectsAsked = useRef('');
  useEffect(() => {
    if (projectsStale && projectsAsked.current !== projectsStale) { projectsAsked.current = projectsStale; onProjectsStale?.(); }
  }, [projectsStale, onProjectsStale]);
  const turnActions = useMemo(() => ({ review, retry, continueTurn, openOutput, openCitation, openShare, redoOutput, chooseOutput, closeOutput, closeCitation,
    setTracePanel: value => { if (value) { closeShare(); closeChoices(); } setTracePanel(value); },
    setDraftPanel: value => { if (value) { closeShare(); closeChoices(); } setDraftPanel(value); },
    setSession, onNavigate, openSettings, askElsewhere, redoAs, projects, refreshPlacement, setError, stopTurn, stops, sending: busy || !sessionReady }),
  [review, retry, continueTurn, openOutput, openCitation, openShare, redoOutput, chooseOutput, closeOutput, closeCitation, closeShare, closeChoices, onNavigate, openSettings, askElsewhere, redoAs, projects, refreshPlacement, setError, stopTurn, stops, busy, sessionReady]);
  const currentStream = streaming?.scope === scope && !stoppedTurn(receiptTurn(visible.turns, streaming.id))
    && !receiptTurn(visible.turns, streaming.id)?.receipt?.ask?.error ? streaming : null;
  const streamProject = currentStream?.project || projectId;
  const streamStopping = Boolean(currentStream && stops[`${streamProject}:${currentStream.id}`]);
  const canStopStream = currentStream && ['ask', 'do'].includes(currentStream.intent)
    && (currentStream.executor || currentStream.receipt?.do?.executor || 'builtin') === 'builtin';
  const empty = !visible.turns.length && !currentStream;
  function startConversation() {
    if (busy || reviewing || retrying || !sessionReady) return;
    for (const channel of ['thread', 'poll', 'outcome-choices', 'project']) gate.invalidate(channel);
    setTracePanel(null); closeCitation(); closeOutput(); closeShare(); setDraftPanel(null);
    setContinuation(null); setChoicePanel(null); setUnknownProject(null); setError('');
    setContinuationReset(value => value + 1);
    externalExecutor.onClose(); attachmentRetry.current = null; sentHere.current.clear();
    writeStoredJson(cacheKey, '');
    setSession(current => ({ ...current, id: null, turns: [] }));
    onNavigate?.('workbench', { project_id: projectId });
  }
  useEffect(() => {
    if (externalExecutor.open && [sharePanel, draftPanel, tracePanel, citationPanel, outputPanel, choicePanel].some(panel => panel?.scope === scope)) externalExecutor.onClose();
  }, [externalExecutor.open, sharePanel, draftPanel, tracePanel, citationPanel, outputPanel, choicePanel, scope]);
  return <div className="workbench-layout"><main className={`workbench ${empty ? 'is-empty' : ''}`} aria-label="工作台">
    {!!visible.threads.length && <div className="workbench-history"><select aria-label="对话" value={visible.id || ''} onChange={event => onNavigate?.('workbench', { thread_id: event.target.value })}>{!visible.id && <option value="">新对话</option>}{visible.threads.map(thread => <option key={thread.id} value={thread.id}>{displayScopedText(thread.title)}</option>)}</select><button type="button" aria-label="新对话" title="新对话" disabled={busy || Boolean(reviewing) || retrying || !sessionReady} onClick={startConversation}>＋</button></div>}
    {empty ? <h1>今天想记住什么？</h1> : <div className="workbench-thread">
      {visible.turns.filter(turn => !(currentStream?.id === turn.id && turn.intent === 'ask')).map(turn => <WorkbenchTurn key={turn.id} turn={turn} turnId={turnId} projectId={projectId} scope={scope} reviewing={reviewing} continuing={busy || retrying || Boolean(reviewing)} streamText={currentStream?.id === turn.id && turn.intent === 'do' ? currentStream.answer : undefined} streamStatus={currentStream?.id === turn.id ? currentStream.status : null} actions={turnActions}/>)}
      {currentStream && !visible.turns.some(turn => turn.id === currentStream.id && turn.intent === 'do') && <article aria-busy="true">
        {currentStream.user_text && <div className="workbench-bubble">{displayScopedText(currentStream.user_text)}</div>}
        <Receipt kind={currentStream.intent || 'ask'}><div className="workbench-receipt-title"><StatusDot state="processing"/>
          {canStopStream && <StopButton disabled={streamStopping} onClick={() => stopTurn(currentStream, streamProject)}/>}
        </div><RetryCountdown status={currentStream.status}/>{currentStream.intent !== 'multi' && <CitedAnswer answer={currentStream.answer}/>}</Receipt>
      </article>}
    </div>}
    <div className="workbench-composer"><WorkbenchError error={error} projectId={error?.projectId} onSettings={onNavigate ? openSettings : undefined} onRetry={threadReadFailure === scope ? retryThreadRead : undefined} retryDisabled={busy || Boolean(reviewing) || retrying}/>{unknownProject?.scope === scope && <button type="button" className="workbench-project-action" disabled={busy || !sessionReady} onClick={createUnknownProject}>{unknownProject.project ? '重发到' : '新建'} #{unknownProject.name}</button>}<Composer key={`${projectId}:${composerRevision}`} resetContinuation={continuationReset} draft={draft} onDraft={onDraft} handoff={handoff} onHandoff={onHandoff} projectTag={projects.find(project => project.id === projectId)?.name || ({ default: '日常', inbox: '收件箱', me: '我' })[projectId] || projectId} onSend={send} tools={({ intent, files }) => <ExternalExecutorButton value={continuation?.scope !== scope && !files.length && (!intent || intent === 'do') ? externalExecutor.value : 'builtin'} onOpen={() => { closeShare(); closeCitation(); closeOutput(); setDraftPanel(null); setTracePanel(null); closeChoices(); externalExecutor.onOpen(); }} unavailable={continuation?.scope !== scope && !files.length && (!intent || intent === 'do') && externalExecutor.unavailable} disabled={busy || !sessionReady || continuation?.scope === scope || !!files.length || (!!intent && intent !== 'do')}/> } prefill={continuation?.scope === scope ? continuation : null} onRecord={record} recording={recording} disabled={busy || !sessionReady}/></div>
  </main>{continuation?.scope !== scope && externalExecutor.open && <ExternalExecutorPicker {...externalExecutor}/>} {sharePanel?.scope === scope && <ConversationSharePreview scope={scope} snapshot={sharePanel.snapshot} loading={sharePanel.loading} error={sharePanel.error} onClose={closeShare} onRetry={() => openShare(sharePanel.turn)}/>} {draftPanel?.scope === scope && tracePanel?.scope !== scope && (draftPanel.documentId
    ? <WorkbenchDraftPanel key={`${projectId}:${draftPanel.documentId}`} projectId={projectId} documentId={draftPanel.documentId} itemId={draftPanel.itemId} onClose={() => setDraftPanel(null)}/>
    : <WorkbenchOriginalPanel key={`${draftPanel.projectId}:${draftPanel.itemId}`} projectId={draftPanel.projectId} itemId={draftPanel.itemId} onClose={() => setDraftPanel(null)}/>)} {outputPanel?.scope === scope && <WorkbenchOutcomePanel projectId={projectId} documentId={outputPanel.documentId} document={outputPanel.document} loading={outputPanel.loading} error={outputPanel.error} task={outputPanel.task} qualified={outputPanel.qualified} previous={outputPanel.previous} onRetryPrevious={reloadOutputPrevious} onContinue={continueOutput} onClose={closeOutput}/>}{tracePanel?.scope === scope && citationPanel?.scope !== scope && <ContextPanel kind={tracePanel.kind || "ask"} receipt={tracePanel.ask} onClose={() => setTracePanel(null)} onOpenCitation={citation => openCitation(citation, 'context')}/>} {citationPanel?.scope === scope && <ContextCitation panel={citationPanel} onClose={closeCitation} onRetry={() => openCitation(citationPanel.citation, citationPanel.origin)}/>}{choicePanel?.scope === scope && <FocusPanel title="成果" onClose={() => { gate.invalidate('outcome-choices'); setChoicePanel(null); }}>{choicePanel.loading ? <StatusDot state="processing"/> : choicePanel.error ? <button type="button" onClick={() => chooseOutput(choicePanel.turn)}>读取未完成 · 重试</button> : (choicePanel.items ?? []).map(row => <Row key={row.document_id} title={row.title} meta={`v${row.version}`} onOpen={() => redoOutput(choicePanel.turn, row.document_id)}/>)}</FocusPanel>}</div>;
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
        {panel.layer === 'insight' ? <p>{entry.text}</p> : <CitationBody text={['source', 'inspiration'].includes(panel.layer) ? entry.text : entry.markdown} coordinateSpace={['source', 'inspiration'].includes(panel.layer) ? entry.coordinate_space : 'document_markdown_v1'} citation={panel.citation}/>}
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
