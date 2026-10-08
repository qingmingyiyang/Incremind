import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Composer, Receipt, InsightChip, ProgressDots, StatusDot, CitedAnswer, LayerBadges, FocusPanel, ConversationSharePreview, MarkdownBody, Icon } from '../../shared/ui';
import { readStoredJson, writeStoredJson } from '../../shared/lib/browserStorage';
import { userStorageKey } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { answerWithoutCitationMarks } from '../../shared/ui/CitedAnswer';
import { createConversationSnapshot } from '../../shared/lib/conversationSnapshot';
import { sendSignal, signalEpoch } from '../../shared/signalsApi';
import { recognitionApi } from '../../shared/api/recognitionApi';
import { workbenchApi } from './workbenchApi';
import { libraryApi } from '../library/libraryApi';
import { ContextPanel } from './ContextPanel';
import { CitationBody } from './CitationBody';
import { WorkbenchDraftPanel } from './WorkbenchDraftPanel';
import { WorkbenchOutcomePanel } from './WorkbenchOutcomePanel';
import { TaskDivision } from './TaskDivision';
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
  const tag = '#([^\\s/#]+)(?:/([^\\s/#]+))?';
  const match = new RegExp('^[ \\t]*' + tag + '(?=[ \\t]|\\r?$)', 'm').exec(text)
    || new RegExp('[ \\t]+' + tag + '[ \\t]*(?=\\r?$)', 'm').exec(text);
  if (!match) return intent === 'inspiration' || (!intent && /^\s*灵感/.test(text)) ? 'inbox' : projectId;
  const choices = new Map(projects.map(project => [project.id, project.name]));
  for (const [id, name] of [['default', '日常'], ['inbox', '收件箱'], ['me', '我']]) if (!choices.has(id)) choices.set(id, name);
  // 旧写法 #默认 仍指日常，除非真有项目叫这个名字。
  if (match[1] === '默认' && !projects.some(project => project.name === '默认')) return 'default';
  if (choices.has(match[1])) return match[1];
  const ids = [...choices].filter(([, name]) => name === match[1]).map(([id]) => id);
  if (ids.length !== 1) {
    const failure = new Error(ids.length ? '项目重名 · 使用项目编号' : '项目未找到 · 修改标签');
    if (!ids.length) failure.projectTag = match[1];
    throw failure;
  }
  return ids[0];
}
const REDO_LABELS = { remember: '换成记住', ask: '换成问', do: '换成干活' };
// 多部分回合里，任一部分还没结束就继续轮询。
const busyReceipt = receipt => receipt.remember?.state === 'processing'
  || ['researching', 'preparing', 'running'].includes(receipt.do?.state)
  || (receipt.parts || []).some(part => ['waiting', 'running'].includes(part.state) || busyReceipt(part.receipt || {}));
const WorkbenchTurn = memo(function WorkbenchTurn({ turn, turnId, projectId, scope, reviewing, continuing, streamText, streamStatus, actions }) {
  const { review, retry, continueTurn, openOutput, openCitation, openShare, redoOutput, chooseOutput, closeOutput, closeCitation,
    setTracePanel, setDraftPanel, setSession, onNavigate, askElsewhere, redoAs, projects, sending, refreshPlacement, setError } = actions;
  const [copied, setCopied] = useState(false), copyTimer = useRef(null), currentScope = useRef(scope), alive = useRef(true);
  const [more, setMore] = useState(false), moreButton = useRef(null);
  currentScope.current = scope;
  useEffect(() => { alive.current = true; setCopied(false); return () => { alive.current = false; clearTimeout(copyTimer.current); }; }, [scope]);
  const recordCopy = (epoch = signalEpoch()) => sendSignal({ kind: 'copy', project_id: projectId, turn_id: turn.id }, epoch);
  async function copyAnswer() {
    const epoch = signalEpoch(), capturedScope = scope;
    try { await navigator.clipboard.writeText(answerWithoutCitationMarks(turn.receipt.ask.answer, turn.receipt.ask.citations)); } catch { return; }
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
        return <article key={turn.id} id={`workbench-${turn.id}`} tabIndex={-1} className={turn.id === turnId ? 'is-target' : ''}>{turn.user_text && <div className="workbench-bubble">{turn.user_text}</div>}
          <div className="workbench-parts" aria-label="拆分">{parts.map(part => <span key={part.index} title={part.span}><Icon name={part.intent} size={12}/>{part.span.length > 12 ? part.span.slice(0, 12) + '…' : part.span}</span>)}{!!redoItems.length && moreMenu(redoItems)}</div>
          {parts.map(part => part.turn_id && part.receipt && Object.keys(part.receipt).length
            ? <WorkbenchTurn key={part.index} turn={{ ...turn, id: part.turn_id, intent: part.intent, user_text: null, receipt: part.receipt }} turnId={turnId} projectId={projectId} scope={scope} reviewing={reviewing} continuing={continuing} actions={actions}/>
            : <Receipt key={part.index} kind={part.intent}><StatusDot state={['failed', 'not_started'].includes(part.state) ? 'failed' : part.state === 'running' ? 'processing' : 'pending'}/></Receipt>)}
        </article>;
      }
      const memory = turn.receipt.remember, inspiration = turn.receipt.inspiration, ask = turn.receipt.ask, task = turn.receipt.do;
      const interruption = (ask || task)?.interruption;
      const interrupted = interruption === 'connection' || interruption === 'sleep';
      const canContinue = Boolean(ask) || task?.state === 'interrupted';
      const hint = ask?.elsewhere, hintName = hint && (projects.find(project => project.id === hint.project_id)?.name || hint.project_id);
      const hintScene = hint?.scene && /^[^\s/#]+$/.test(hint.scene) ? hint.scene : null;
      const hintLabel = hint && `#${hintName}${hintScene ? '/' + hintScene : ''}`;
      const shareable = typeof ask?.answer === 'string' || typeof inspiration?.insight?.text === 'string'
        || Boolean(memory?.state === 'done' && memory.document_id)
        || Boolean(['done', 'partial'].includes(task?.state) && task.document_id);
      const shareMenu = moreMenu(<><button type="button" role="menuitem" disabled={!shareable} title={shareable ? undefined : '完成后分享'} onClick={() => { setMore(false); moreButton.current?.focus(); openShare(turn); }}>分享</button>{redoItems}</>);
      return <article key={turn.id} id={`workbench-${turn.id}`} tabIndex={-1} className={turn.id === turnId ? 'is-target' : ''}>{turn.user_text && <div className="workbench-bubble">{turn.user_text}</div>}<Receipt kind={turn.intent}>{memory ? <><div className="workbench-receipt-title"><StatusDot state={memory.state}/><span className="workbench-memory-title" title={memory.title}>{memory.title}</span><ProgressDots {...memory.progress} running={memory.state === 'processing'} title="原件 · 正文 · 整理 · 入库"/>{memory.document_id && <button type="button" aria-label="打开整理稿" onClick={() => { closeOutput(); closeCitation(); setTracePanel(null); setDraftPanel({ scope, documentId: memory.document_id, itemId: memory.item_id }); }}>↗</button>}{memory.state === 'failed' && <button type="button" onClick={() => retry(turn)}>重试</button>}</div><DocumentPlacement projectId={projectId} projects={projects} row={{...memory,revision:memory.document_revision}} onChanged={refreshPlacement} onError={setError}/><div className="workbench-insights">{memory.insights.map(insight => chip(insight, memory.filing?.state === 'filed' ? memory.filing.target_project_id : undefined))}{!!memory.related.length && <span className="workbench-related-count" title={memory.related.map(entry => entry.text).join('\n')}>∞ {memory.related.length}</span>}</div>{memory.error && <div className="workbench-error">{({ remote_disabled: '模型外发已关闭 · 查看设置', private_project_remote_blocked: '项目为私密 · 查看设置', interrupted: '处理已中断 · 重试' })[memory.error] || '整理未完成 · 重试'}</div>}</> : ask ? <><CitedAnswer answer={ask.answer ?? ask.partial ?? ''} citations={ask.citations} onOpenCitation={openCitation} citationHref="#workbench-citation" onCopyAnswer={() => recordCopy()}/><div className="workbench-ask-layers"><LayerBadges layers={ask.layers} onOpenTrace={() => { closeOutput(); closeCitation(); setTracePanel({ scope, ask }); }}/>{hint && <span className="ui-layer-badges workbench-elsewhere"><button type="button" title={`在 ${hintLabel} 里问`} aria-label={`在 ${hintLabel} 里问`} disabled={sending} onClick={() => askElsewhere(turn, hint)}>→ {hintLabel}</button></span>}<button type="button" className="workbench-answer-copy" aria-label="复制回答" title="复制" onClick={copyAnswer}><Icon name={copied ? 'check' : 'copy'} size={14}/></button></div></> : task ? <><div className="workbench-receipt-title"><StatusDot state={['researching', 'preparing', 'running'].includes(task.state) ? 'processing' : task.state === 'waiting_approval' ? 'pending' : task.state}/><span className="workbench-memory-title" title={task.title}>{task.title}</span><button type="button" className="workbench-context-open" aria-label="本次上下文" onClick={() => { closeOutput(); closeCitation(); setTracePanel({ scope, ask: task, kind: "do" }); }}>◯ {contextPercent(task.context)}</button><ProgressDots {...task.progress} running={['researching', 'preparing', 'running'].includes(task.state)} title={task.kernel_turn_id ? '拆活 · 干活 · 汇总' : task.research_turn_id ? '研究 · 准备 · 批准 · 成果' : '准备 · 批准 · 成果'}/><OutcomeVersion version={task.continues?.version}/>{task.fallback_new && <span className="ui-count" aria-label="已新写一篇">新写一篇</span>}{['done','partial'].includes(task.state) && task.document_id && <OutcomeReceiptChoices turn={turn} onNew={() => redoOutput(turn, null)} onOther={() => chooseOutput(turn)}/>}{['done','partial'].includes(task.state) && task.document_id && <button type="button" aria-label="打开成果" onClick={() => openOutput(task)}>↗</button>}</div>{task.kernel_turn_id && <TaskDivision key={`${projectId}:${turn.id}`} project={projectId} turnId={turn.id} task={task} onReference={reference => onNavigate?.('workbench', reference)} onRedo={result => setSession(current => current.project === projectId ? { ...current, id: result.thread_id, turns: [...current.turns, result.turn] } : current)}/>}{!task.kernel_turn_id && !!task.experts?.length && <div className="workbench-experts">{task.experts.map((expert, index) => <span key={index} role="img" aria-label={expert.role} title={expert.role}><StatusDot aria-hidden="true" state={expert.state === 'running' ? 'processing' : expert.state}/></span>)}</div>}</> : inspiration?.insight ? <>{chip(inspiration.insight)}{inspiration.placement && projects.some(project=>project.id===inspiration.placement.project_id) && <InboxFiling key={`${scope}:${inspiration.insight.id}:${inspiration.insight.revision}`} row={inspiration.insight} targetProjectId={inspiration.placement.project_id} scenes={projects.find(project=>project.id===inspiration.placement.project_id)?.scenes || []} suggestion={inspiration.placement.scene} onFiled={refreshPlacement} onError={setError}/>}</> : null}{task && interrupted && <><RetryCountdown status={streamStatus}/><CitedAnswer answer={streamText ?? task.partial ?? ''}/></>} {interrupted && <div className="workbench-interrupted"><span>{interruption === 'sleep' ? '电脑休眠' : '连接中断'}</span>{canContinue && <><span aria-hidden="true">·</span><button type="button" disabled={continuing} onClick={() => continueTurn(turn)}>继续</button></>}</div>}</Receipt>{shareMenu}</article>;
});

export function Workbench({ projectId = 'default', projects = EMPTY_PROJECTS, threadId, turnId, onNavigate, draft, onDraft, handoff, onHandoff, onProjectCreated, onProjectsStale }) {
  const gate = useRequestScope(`${projectId}:${threadId || ''}:${turnId || ''}`), scope = gate.scope;
  const [session, setSession] = useState({ scope: threadId || turnId ? null : scope, project: projectId, threads: [], id: null, turns: [] });
  const sessionReady = session.scope === scope && session.project === projectId;
  const visible = sessionReady ? session : { threads: [], id: null, turns: [] };
  const [error, setError] = useState(''), [busy, setBusy] = useState(false), [reviewing, setReviewing] = useState(null);
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
  const [unknownProject, setUnknownProject] = useState(null), [composerRevision, setComposerRevision] = useState(0);
  const creatingProject = useRef(false);
  const streamController = useRef(null);
  const resumedTurns = useRef({ scope: null, ids: new Set() });
  const recorderRef = useRef(null), recordingResolveRef = useRef(null), startingRecord = useRef(false);
  const cacheKey = userStorageKey(`chriptmas-v2-thread:${projectId}`);
  useEffect(() => () => { streamController.current?.abort(); }, [scope]);
  useEffect(() => {
    outcomeRedoPending.current = null;
    setError(''); setBusy(false); setReviewing(null); setRetrying(false); setRecording(false); setTracePanel(null); setCitationPanel(null); setOutputPanel(null); setDraftPanel(null); setSharePanel(null); setStreaming(null); setUnknownProject(null); setContinuation(null); setChoicePanel(null); creatingProject.current = false;
    const request = gate.issue('thread', scope);
    async function load() {
      try {
        const list = await workbenchApi.threads(projectId);
        if (!gate.isCurrent(request)) return;
        const threads = list.items || [];
        let id = threadId || readStoredJson(cacheKey).value;
        if (!threads.some(thread => thread.id === id)) id = threads[0]?.id || null;
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
        if (id) writeStoredJson(cacheKey, id);
      } catch (failure) { if (gate.isCurrent(request)) setError(failure.message); }
    }
    load();
    return () => gate.invalidate('thread');
  }, [projectId, threadId, turnId]);
  useEffect(() => {
    if (!sessionReady || !visible.id) return;
    if (resumedTurns.current.scope !== scope) resumedTurns.current = { scope, ids: new Set() };
    const turn = [...visible.turns].reverse().find(value =>
      (value.receipt.ask?.interruption || value.receipt.do?.state === 'interrupted')
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
      } catch (failure) { if (gate.isCurrent(request)) setError(failure.message); }
      finally {
        if (streamController.current === controller) streamController.current = null;
        if (gate.isCurrent(request)) { setBusy(false); setStreaming(null); }
      }
    }
    read();
    return () => { controller.abort(); gate.invalidate('resume'); };
  }, [scope, projectId, sessionReady, visible.id]);
  const processing = visible.turns.some(turn => busyReceipt(turn.receipt));
  useEffect(() => {
    if (!processing || !visible.id || busy || reviewing || retrying) return;
    const id = visible.id;
    let timer, cancelled = false;
    async function poll() {
      const request = gate.issue('poll', scope);
      try {
        const detail = await workbenchApi.thread(projectId, id);
        if (cancelled || !gate.isCurrent(request)) return;
        setSession(current => ({ ...current, turns: detail.turns }));
        if (detail.turns.some(turn => busyReceipt(turn.receipt))) timer = setTimeout(poll, 1500);
      } catch (failure) { if (!cancelled && gate.isCurrent(request)) { setError(failure.message); timer = setTimeout(poll, 1500); } }
    }
    timer = setTimeout(poll, 1500);
    return () => { cancelled = true; clearTimeout(timer); gate.invalidate('poll'); };
  }, [projectId, threadId, turnId, visible.id, processing, busy, reviewing, retrying]);
  useEffect(() => {
    if (!turnId) return;
    document.getElementById(`workbench-${turnId}`)?.scrollIntoView?.({ block: 'center' });
  }, [turnId, visible.turns]);
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
  async function send({ text, files, intent }, createdProject) {
    if ((busy && !createdProject) || !sessionReady) return false;
    const request = gate.issue('send', scope);
    gate.invalidate('thread'); gate.invalidate('poll');
    setBusy(true); setError(''); setUnknownProject(null);
    const controller = new AbortController(); streamController.current = controller;
    try {
      const project = createdProject || targetProject(text, intent, projectId, projects);
      const threadCacheKey = userStorageKey(`chriptmas-v2-thread:${project}`);
      let id = project === projectId ? visible.id : null;
      const images = files.length > 1 && files.every(file => /\.(png|jpe?g|bmp|tiff?|webp)$/i.test(file.name));
      const batches = images ? [files] : files.map(file => [file]);
      if (!batches.length) batches.push([]);
      for (const batch of batches) {
        const [file, ...others] = batch;
        const item = file ? await workbenchApi.file(project, file, others) : null;
        if (!gate.isCurrent(request)) return;
        const result = await workbenchApi.create({ project_id: project, ...(id ? { thread_id: id } : {}), text, ...(file ? { intent: 'remember' } : intent ? { intent } : {}), ...(item ? { item_id: item.id } : {}),
          ...(!file && intent === 'do' && project === projectId && continuation?.scope === scope ? { continue_from: continuation.documentId } : {}) }, {
          signal: controller.signal,
          onStarted: value => { if (gate.isCurrent(request)) { setStreaming({ scope, ...value.turn, answer: '' }); if (typeof value.thread_id === 'string') writeStoredJson(threadCacheKey, value.thread_id); } },
          onDelta: delta => { if (gate.isCurrent(request)) setStreaming(current => current?.scope === scope ? { ...current, answer: current.answer + delta, status: null } : current); },
          onReset: value => { if (gate.isCurrent(request) && typeof value.text === 'string') setStreaming(current => current?.scope === scope ? { ...current, answer: value.text, status: null } : current); },
          onStatus: value => { if (gate.isCurrent(request)) setStreaming(current => current?.scope === scope ? { ...current, status: value.state === 'retrying' ? value : null } : current); },
        });
        if (!gate.isCurrent(request)) return;
        id = result.thread_id;
        writeStoredJson(threadCacheKey, id);
        const favorites = !file && (!intent || intent === 'remember') && /https:\/\/(?:space\.bilibili\.com\/\d+\/favlist|(?:www\.)?bilibili\.com\/medialist\/detail\/ml\d+)/.test(text);
        const expanded = favorites ? await workbenchApi.thread(project, id) : null;
        if (!gate.isCurrent(request)) return;
        if (project === 'default' && result.turn?.id) sentHere.current.add(result.turn.id);
        if (project === projectId) setSession(current => ({ scope, project, id, threads: current.threads.some(thread => thread.id === id) ? current.threads : [{ id, title: result.turn.user_text || file?.name || '' }, ...current.threads], turns: expanded?.turns || (current.id === id ? [...current.turns, result.turn] : [result.turn]) }));
      }
      setContinuation(null);
      if (project !== projectId || id !== visible.id) onNavigate?.('workbench', { project_id: project, thread_id: id });
      return true;
    } catch (failure) { if (gate.isCurrent(request)) {
      setError(failure.message);
      if (failure.projectTag) setUnknownProject({ scope, name: failure.projectTag, input: { text, files, intent } });
    } return false; }
    finally { if (streamController.current === controller) streamController.current = null; if (gate.isCurrent(request)) { setBusy(false); setStreaming(null); } }
  }
  const askElsewhere = useCallback((turn, hint) => {
    if (!gate.isScopeCurrent(scope)) return;
    const scene = hint.scene && /^[^\s/#]+$/.test(hint.scene) ? '/' + hint.scene : '';
    const name = projects.find(project => project.id === hint.project_id)?.name;
    const tag = name && /^[^\s/#]+$/.test(name) && projects.filter(project => project.name === name).length === 1 ? name : hint.project_id;
    return send({ text: `#${tag}${scene}\n${turn.user_text}`, files: [], intent: 'ask' });
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
      if (gate.isCurrent(request)) {
        if (accepted) setComposerRevision(value => value + 1);
        else setUnknownProject({ ...retained, project });
      }
    } catch (failure) { if (gate.isCurrent(request)) setError(failure.message); }
    finally { if (gate.isCurrent(request)) { creatingProject.current = false; setBusy(false); } }
  }
  const refreshPlacement = useCallback(async () => {
    if (!visible.id) return;
    const request = gate.issue('placement', scope);
    try {
      const detail = await workbenchApi.thread(projectId, visible.id);
      if (gate.isCurrent(request)) setSession(current => ({ ...current, turns: detail.turns }));
    } catch (failure) { if (gate.isCurrent(request)) setError(failure.message); }
  }, [gate, scope, projectId, visible.id]);
  const review = useCallback(async (insight, action, project = projectId) => {
    if (reviewing) return;
    const request = gate.issue('review', scope); setReviewing(insight.id); setError('');
    gate.invalidate('thread'); gate.invalidate('poll');
    try {
      const updated = await workbenchApi.insight(project, insight, action);
      if (!gate.isCurrent(request)) return;
      setSession(current => ({ ...current, turns: current.turns.map(turn => !turn.receipt.remember && !turn.receipt.inspiration ? turn : ({ ...turn, receipt: turn.receipt.remember ? { remember: { ...turn.receipt.remember, insights: turn.receipt.remember.insights.flatMap(old => old.id !== insight.id ? [old] : action === 'drop' ? [] : [updated]) } } : { inspiration: { insight: turn.receipt.inspiration.insight?.id === insight.id ? (action === 'drop' ? null : updated) : turn.receipt.inspiration.insight } } })) }));
    } catch (failure) { if (gate.isCurrent(request)) setError(failure.message); }
    finally { if (gate.isCurrent(request)) setReviewing(null); }
  }, [reviewing, gate, scope, projectId]);
  const retry = useCallback(async (turn) => {
    if (retrying) return;
    const request = gate.issue('retry', scope);
    gate.invalidate('thread'); gate.invalidate('poll'); setRetrying(true);
    try { const updated = await workbenchApi.retry(projectId, turn.id); if (gate.isCurrent(request)) setSession(current => ({ ...current, turns: current.turns.map(old => old.id === turn.id ? updated : old) })); }
    catch (failure) { if (gate.isCurrent(request)) setError(failure.message); }
    finally { if (gate.isCurrent(request)) setRetrying(false); }
  }, [retrying, gate, scope, projectId]);
  const continueTurn = useCallback(async (turn) => {
    if (busy || retrying || reviewing) return;
    const request = gate.issue('continue', scope);
    gate.invalidate('thread'); gate.invalidate('poll'); setBusy(true); setError('');
    const controller = new AbortController(); streamController.current = controller;
    try {
      const updated = await workbenchApi.continue(projectId, turn.id, { signal: controller.signal });
      if (gate.isCurrent(request)) setSession(current => ({ ...current,
        turns: current.turns.map(old => old.id === turn.id ? updated : old) }));
    } catch (failure) { if (gate.isCurrent(request)) setError(failure.message); }
    finally {
      if (streamController.current === controller) streamController.current = null;
      if (gate.isCurrent(request)) setBusy(false);
    }
  }, [busy, retrying, reviewing, gate, scope, projectId]);
  const openOutput = useCallback(async (task) => {
    gate.invalidate('share'); setSharePanel(null);
    setDraftPanel(null);
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
  }, [gate, scope, projectId]);
  const openCitation = useCallback(async (citation) => {
    gate.invalidate('share'); setSharePanel(null);
    setDraftPanel(null);
    if (!['insight', 'summary', 'note', 'source', 'inspiration'].includes(citation.layer) || !citation.id) return;
    const request = gate.issue('citation', scope), project = citation.persona ? 'me' : citation.project || projectId, signalToken = signalEpoch();
    setCitationPanel({ scope, layer: citation.layer, citation, loading: true });
    if (citation.layer === 'source') {
      const [drill, full] = await Promise.allSettled([
        libraryApi.drill(project, 'source', citation.id), libraryApi.sourceText(project, citation.id),
      ]);
      if (!gate.isCurrent(request)) return;
      const original = full.status === 'fulfilled' && typeof full.value.text === 'string' ? full.value : null;
      setCitationPanel({ scope, layer: citation.layer, citation, data: drill.status === 'fulfilled' ? drill.value : null, original, error: !original });
    } else {
      try {
        const data = await libraryApi.drill(project, citation.layer === 'summary' ? 'note' : citation.layer, citation.id);
        if (gate.isCurrent(request)) {
          setCitationPanel({ scope, layer: citation.layer, citation, data, project, signalToken });
          libraryApi.opened(project, citation.layer, citation.id);
        }
      } catch { if (gate.isCurrent(request)) setCitationPanel({ scope, layer: citation.layer, citation, error: true }); }
    }
  }, [gate, scope, projectId]);
  useEffect(() => {
    const insight = citationPanel?.data?.insight;
    if (citationPanel?.scope === scope && citationPanel.layer === 'insight' && !citationPanel.loading && !citationPanel.error && insight?.state === 'pending')
      sendSignal({ kind: 'view', project_id: citationPanel.project, object: { kind: 'insight', id: insight.id, revision: insight.revision } }, citationPanel.signalToken);
  }, [citationPanel, scope]);
  const closeCitation = useCallback(() => { gate.invalidate('citation'); setCitationPanel(null); }, [gate]);
  const closeOutput = useCallback(() => { gate.invalidate('output'); setOutputPanel(null); }, [gate]);
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
    } catch (failure) { if (gate.isCurrent(token)) setError(failure.message); }
    finally { if (outcomeRedoPending.current === token) outcomeRedoPending.current = null; if (gate.isCurrent(token)) setRetrying(false); }
  }, [gate, scope, projectId, retrying]);
  const chooseOutput = useCallback(async (turn) => {
    gate.invalidate('share'); setSharePanel(null);
    const token = gate.issue('outcome-choices', scope);
    if (!token) return;
    closeOutput(); setTracePanel(null); closeCitation();
    setChoicePanel({ scope, turn, loading: true });
    try {
      const result = await libraryApi.outcomeChoices(projectId);
      if (gate.isCurrent(token)) setChoicePanel({ scope, turn, items: result.items });
    } catch { if (gate.isCurrent(token)) setChoicePanel({ scope, turn, error: true }); }
  }, [gate, scope, projectId, closeOutput, closeCitation]);
  function continueOutput() {
    if (!gate.isScopeCurrent(scope) || outputPanel?.scope !== scope || !outputPanel.documentId || !outputPanel.qualified) return;
    setContinuation({ scope, id: ++continuationSequence.current, documentId: outputPanel.documentId,
      text: `接着《${outputPanel.document.title || outputPanel.task.title}》写：`, intent: 'do' });
    closeOutput();
  }
  const closeShare = useCallback(() => { gate.invalidate('share'); setSharePanel(null); }, [gate]);
  const openShare = useCallback(async (turn) => {
    gate.invalidate('outcome-choices'); setChoicePanel(null);
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
      const snapshot = createConversationSnapshot({ question: turn.user_text || '', answer, citations });
      if (gate.isCurrent(request)) setSharePanel({ scope, turn, snapshot });
    } catch { if (gate.isCurrent(request)) setSharePanel({ scope, turn, error: true }); }
  }, [gate, scope, projectId, closeOutput, closeCitation]);
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
    setTracePanel: value => { if (value) closeShare(); setTracePanel(value); },
    setDraftPanel: value => { if (value) closeShare(); setDraftPanel(value); },
    setSession, onNavigate, askElsewhere, redoAs, projects, refreshPlacement, setError, sending: busy || !sessionReady }),
  [review, retry, continueTurn, openOutput, openCitation, openShare, redoOutput, chooseOutput, closeOutput, closeCitation, closeShare, onNavigate, askElsewhere, redoAs, projects, refreshPlacement, busy, sessionReady]);
  const currentStream = streaming?.scope === scope ? streaming : null;
  const empty = !visible.turns.length && !currentStream;
  return <div className="workbench-layout"><main className={`workbench ${empty ? 'is-empty' : ''}`} aria-label="工作台">
    {!!visible.threads.length && <div className="workbench-history"><select aria-label="对话" value={visible.id || ''} onChange={event => onNavigate?.('workbench', { thread_id: event.target.value })}>{!visible.id && <option value="">新对话</option>}{visible.threads.map(thread => <option key={thread.id} value={thread.id}>{thread.title}</option>)}</select><button type="button" aria-label="新对话" title="新对话" disabled={busy || Boolean(reviewing) || retrying} onClick={() => { gate.invalidate('thread'); gate.invalidate('poll'); setTracePanel(null); closeCitation(); closeOutput(); closeShare(); setDraftPanel(null); setSession(current => ({ ...current, id: null, turns: [] })); }}>＋</button></div>}
    {empty ? <h1>今天想记住什么？</h1> : <div className="workbench-thread">{visible.turns.filter(turn => !(currentStream?.id === turn.id && turn.intent === 'ask')).map(turn => <WorkbenchTurn key={turn.id} turn={turn} turnId={turnId} projectId={projectId} scope={scope} reviewing={reviewing} continuing={busy || retrying || Boolean(reviewing)} streamText={currentStream?.id === turn.id && turn.intent === 'do' ? currentStream.answer : undefined} streamStatus={currentStream?.id === turn.id ? currentStream.status : null} actions={turnActions}/>)}{currentStream && !visible.turns.some(turn => turn.id === currentStream.id && turn.intent === 'do') && <article aria-busy="true">{currentStream.user_text && <div className="workbench-bubble">{currentStream.user_text}</div>}<Receipt kind={currentStream.intent || "ask"}><StatusDot state="processing"/><RetryCountdown status={currentStream.status}/>{currentStream.intent !== 'multi' && <CitedAnswer answer={currentStream.answer}/>}</Receipt></article>}</div>}
    <div className="workbench-composer">{error && <p role="alert" className="workbench-error">{error}</p>}{unknownProject?.scope === scope && <button type="button" disabled={busy || !sessionReady} onClick={createUnknownProject}>{unknownProject.project ? '重发到' : '新建'} #{unknownProject.name}</button>}<Composer key={`${projectId}:${composerRevision}`} draft={draft} onDraft={onDraft} handoff={handoff} onHandoff={onHandoff} projectTag={projects.find(project => project.id === projectId)?.name || ({ default: '日常', inbox: '收件箱', me: '我' })[projectId] || projectId} onSend={send} prefill={continuation?.scope === scope ? continuation : null} onRecord={record} recording={recording} disabled={busy || !sessionReady}/></div>
  </main>{sharePanel?.scope === scope && <ConversationSharePreview scope={scope} snapshot={sharePanel.snapshot} loading={sharePanel.loading} error={sharePanel.error} onClose={closeShare} onRetry={() => openShare(sharePanel.turn)}/>} {draftPanel?.scope === scope && tracePanel?.scope !== scope && <WorkbenchDraftPanel key={`${projectId}:${draftPanel.documentId}`} projectId={projectId} documentId={draftPanel.documentId} itemId={draftPanel.itemId} onClose={() => setDraftPanel(null)}/>} {outputPanel?.scope === scope && <WorkbenchOutcomePanel projectId={projectId} documentId={outputPanel.documentId} document={outputPanel.document} loading={outputPanel.loading} error={outputPanel.error} task={outputPanel.task} qualified={outputPanel.qualified} previous={outputPanel.previous} onRetryPrevious={reloadOutputPrevious} onContinue={continueOutput} onClose={closeOutput}/>}{tracePanel?.scope === scope && citationPanel?.scope !== scope && <ContextPanel kind={tracePanel.kind || "ask"} receipt={tracePanel.ask} onClose={() => setTracePanel(null)} onOpenCitation={openCitation}/>} {citationPanel?.scope === scope && <ContextCitation panel={citationPanel} onClose={closeCitation} onRetry={() => openCitation(citationPanel.citation)}/>}{choicePanel?.scope === scope && <FocusPanel title="成果" onClose={() => { gate.invalidate('outcome-choices'); setChoicePanel(null); }}>{choicePanel.loading ? <StatusDot state="processing"/> : choicePanel.error ? <button type="button" onClick={() => chooseOutput(choicePanel.turn)}>读取未完成 · 重试</button> : (choicePanel.items ?? []).map(row => <Row key={row.document_id} title={row.title} meta={`v${row.version}`} onOpen={() => redoOutput(choicePanel.turn, row.document_id)}/>)}</FocusPanel>}</div>;
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
