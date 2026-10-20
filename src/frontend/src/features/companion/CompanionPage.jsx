import { useEffect, useMemo, useRef, useState } from "react";
import { Icon, MarkdownBody, StatusDot } from "../../shared/ui";
import { createCompanionApi } from "./companionApi";
import { userStorageKey } from "../../shared/api/deviceTransport";
import { readStoredJson, writeStoredJson, removeStoredItem } from "../../shared/lib/browserStorage";
import { useRequestScope } from "../../shared/lib/useRequestScope";
import "./companionPage.css";
const MODES = [["chat", "聊聊"], ["review", "回顾"], ["focus", "专注"], ["plan", "安排"]];
const LINES = { chat: "在呢。", review: "看看这一周。", focus: "我陪你专注一会儿。", plan: "从记住的事开始。" };
export function CompanionPage({ projectId = "", api: injectedApi, initialMode = "chat", initialPrompt = "", initialMemoryId = "" }) {
  const api = useMemo(() => injectedApi || createCompanionApi(), [injectedApi]);
  const [mode, setMode] = useState(initialMode);
  const [userScope, setUserScope] = useState(() => userStorageKey("companion-chat"));
  useEffect(() => {
    const changed = () => setUserScope(userStorageKey("companion-chat"));
    window.addEventListener("chriptmas-user-space", changed);
    window.addEventListener("chriptmas-device-required", changed);
    return () => {
      window.removeEventListener("chriptmas-user-space", changed);
      window.removeEventListener("chriptmas-device-required", changed);
    };
  }, []);
  return <main className="companion-page" aria-label="伙伴">
    <aside className="companion-page-bear"><div className="companion-page-sprite" data-mode={mode} role="img" aria-label="小熊"/><p>{LINES[mode]}</p>{typeof globalThis.electronAPI?.enterCompanionMode === "function" && <button type="button" className="companion-page-pet" onClick={() => globalThis.electronAPI.enterCompanionMode()}>显示桌宠</button>}</aside>
    <section className="companion-page-content"><nav className="companion-page-modes" aria-label="伙伴模式">{MODES.map(([key, label]) => <button key={key} type="button" aria-pressed={mode === key} onClick={() => setMode(key)}>{label}</button>)}</nav>
    <div className="companion-page-mode" hidden={mode !== "chat"} key={`chat:${userScope}:${projectId}`}><Chat api={api} projectId={projectId} cacheKey={`${userScope}:${projectId || "default"}`} initialPrompt={initialPrompt} initialMemoryId={initialMemoryId}/></div>
    <div className="companion-page-mode" hidden={mode === "chat"} key={`mode:${mode}:${projectId}`}>
      {mode === "review" && <Review api={api} projectId={projectId}/>}
      {mode === "focus" && <Focus api={api}/>}
      {mode === "plan" && <Todos api={api} projectId={projectId}/>}
    </div></section>
  </main>;
}
function useLive() {
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  return alive;
}
function Retry({ onClick, label = "重试" }) { return <button type="button" className="companion-page-retry" aria-label={label} onClick={onClick}><StatusDot state="failed"/></button>; }
function Review({ api, projectId }) {
  const [stats, setStats] = useState(null), [error, setError] = useState(false), [attempt, setAttempt] = useState(0);
  useEffect(() => { const c = new AbortController(); let live = true;
    api.getWeekStats({ projectId, signal: c.signal }).then(value => { if (live) { setStats(value); setError(false); } }).catch(() => { if (live) setError(true); });
    return () => { live = false; c.abort(); };
  }, [api, projectId, attempt]);
  return <section aria-label="本周回顾"><div className="companion-page-stats">{[["remember", "记住"], ["confirm", "确认"], ["forget", "遗忘"]].map(([key, label]) => <div key={key}><strong>{stats?.[key] ?? "—"}</strong><span>{label}</span></div>)}</div><p className="companion-page-week">本周</p>{error && <Retry onClick={() => setAttempt(x => x + 1)}/>}</section>;
}
function shanghaiDate(value) {
  const date = new Date(value); return Number.isNaN(date.getTime()) ? "" : new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" }).format(date);
}
function Todos({ api, projectId }) {
  const [items, setItems] = useState([]), [failed, setFailed] = useState({}), [busy, setBusy] = useState({}), [error, setError] = useState(false), [attempt, setAttempt] = useState(0);
  const live = useLive(), pending = useRef(new Set());
  useEffect(() => { const c = new AbortController(); let active = true;
    api.listTodos({ projectId, includeDone: true, signal: c.signal }).then(value => { if (active) { setItems(value.items || []); setError(false); } }).catch(() => { if (active) setError(true); });
    return () => { active = false; c.abort(); };
  }, [api, projectId, attempt]);
  async function toggle(item, done = !item.done) {
    if (pending.current.has(item.id)) return;
    pending.current.add(item.id); setBusy(x => ({ ...x, [item.id]: true }));
    setFailed(x => ({ ...x, [item.id]: null }));
    setItems(rows => rows.map(row => row.id === item.id ? { ...row, done, done_at: done ? new Date().toISOString() : null } : row));
    try {
      const result = await (done ? api.doneTodo : api.undoTodo)({ id: item.id, expectedRevision: item.revision ?? null });
      if (live.current) setItems(rows => rows.map(row => row.id === item.id ? { ...row, ...result } : row));
    } catch {
      if (live.current) { setItems(rows => rows.map(row => row.id === item.id ? item : row)); setFailed(x => ({ ...x, [item.id]: { item, done } })); }
    } finally { pending.current.delete(item.id); if (live.current) setBusy(x => ({ ...x, [item.id]: false })); }
  }
  async function retry(item) {
    if (pending.current.has(item.id)) return;
    pending.current.add(item.id); setBusy(x => ({ ...x, [item.id]: true }));
    const desired = failed[item.id]?.done;
    try {
      const value = await api.listTodos({ projectId, includeDone: true });
      if (!live.current) return;
      const current = value.items?.find(row => row.id === item.id);
      pending.current.delete(item.id);
      if (!current) { setItems(rows => rows.filter(row => row.id !== item.id)); return; }
      setItems(rows => rows.map(row => row.id === item.id ? current : row));
      await toggle(current, desired);
    } catch { /* Keep the same retry dot if the fresh read fails. */ }
    finally { pending.current.delete(item.id); if (live.current) setBusy(x => ({ ...x, [item.id]: false })); }
  }
  const today = shanghaiDate(Date.now());
  return <section aria-label="待办安排" className="companion-page-todos">{items.filter(item => !item.done || shanghaiDate(item.done_at) === today).map(item => <div key={item.id} className="companion-page-todo" data-done={item.done}><label><input type="checkbox" aria-label={item.text} checked={item.done} disabled={Boolean(busy[item.id])} onChange={() => toggle(item)}/><span>{item.text}</span></label><small>{item.project_name}</small>{failed[item.id] && <Retry label={`重试${item.text}`} onClick={() => retry(item)}/>}</div>)}{error && <Retry onClick={() => setAttempt(x => x + 1)}/>}</section>;
}
const CHAT_ID = /^[a-z0-9][a-z0-9:_-]{0,127}$/;
function chatRecovery(value, projectId) {
  const request = value?.request;
  return request && typeof request.requestId === "string" && CHAT_ID.test(request.requestId) && typeof request.text === "string" && Boolean(request.text.trim())
    && request.text === request.text.trim() && request.text.length <= 4000 && typeof request.projectId === "string"
    && (request.projectId || "default") === (projectId || "default")
    && (request.sessionId === null || typeof request.sessionId === "string" && CHAT_ID.test(request.sessionId))
    && (request.memoryId === null || typeof request.memoryId === "string" && /^[A-Za-z0-9][A-Za-z0-9:_-]{0,191}$/.test(request.memoryId))
    && typeof value.submittedText === "string" && value.submittedText.trim() === request.text && typeof value.draft === "string" ? value : null;
}
const sameChatRequest = (left, right) => Boolean(left?.request && right?.request)
  && ["requestId", "text", "projectId", "sessionId", "memoryId"].every(key => left.request[key] === right.request[key]);
function mergeChatMessages(current, additions) {
  const messages = new Map(current.map(message => [message.message_id, message]));
  for (const message of additions) if (message) messages.set(message.message_id, message);
  return [...messages.values()];
}
function Chat({ api, projectId, cacheKey, initialPrompt, initialMemoryId }) {
  const [storage] = useState(() => { try { return globalThis.sessionStorage || null; } catch { return null; } });
  const recoveryKey = `${cacheKey}:pending`;
  const [recovery] = useState(() => chatRecovery(readStoredJson(recoveryKey, storage).value, projectId));
  const [text, setText] = useState(recovery?.draft ?? initialPrompt), [messages, setMessages] = useState([]), [busy, setBusy] = useState(false), [failed, setFailed] = useState(Boolean(recovery));
  const session = useRef(recovery ? recovery.request.sessionId : undefined), pending = useRef(recovery), sending = useRef(false), writer = useRef(null), composing = useRef(false);
  const currentText = useRef(text); currentText.current = text;
  if (session.current === undefined) {
    const stored = readStoredJson(cacheKey).value;
    session.current = typeof stored === "string" && CHAT_ID.test(stored) ? stored : null;
  }
  const [loading, setLoading] = useState(Boolean(session.current)), [historyFailed, setHistoryFailed] = useState(false), [attempt, setAttempt] = useState(0);
  const gate = useRequestScope(cacheKey), scope = gate.scope;
  const isCurrent = token => gate.isCurrent(token) && cacheKey === `${userStorageKey("companion-chat")}:${projectId || "default"}`;
  function clearPending(expected) {
    if (sameChatRequest(readStoredJson(recoveryKey, storage).value, expected)) removeStoredItem(recoveryKey, storage);
    if (sameChatRequest(pending.current, expected)) pending.current = null;
  }
  function changeText(value) {
    currentText.current = value; setText(value);
    if (pending.current) {
      pending.current = { ...pending.current, draft: value };
      writeStoredJson(recoveryKey, pending.current, storage);
    }
  }
  function forgetSession(expected) {
    if (readStoredJson(cacheKey).value === expected) removeStoredItem(cacheKey);
    session.current = null; setMessages([]);
  }
  function acceptMessages(entry, receivedSession, pair) {
    const request = entry.request, identity = receivedSession?.session_id, project = request.projectId || "default";
    if (typeof identity !== "string" || !CHAT_ID.test(identity) || receivedSession.project_id !== project
        || !Number.isSafeInteger(receivedSession.context_epoch) || receivedSession.context_epoch < 0
        || request.sessionId !== null && identity !== request.sessionId || pair.length !== 2
        || !pair.every((message, index) => message && typeof message.message_id === "string" && message.message_id
          && message.request_id === request.requestId && message.project_id === project && message.status === "completed"
          && Number.isSafeInteger(message.context_epoch) && message.context_epoch >= 0 && message.context_epoch <= receivedSession.context_epoch
          && message.context_epoch === pair[0].context_epoch && message.role === (index ? "assistant" : "user")
          && typeof message.content === "string") || pair[0].message_id === pair[1].message_id || pair[0].content !== request.text) {
      throw new Error("chat_response_mismatch");
    }
    gate.invalidate("history"); setLoading(false);
    session.current = identity; writeStoredJson(cacheKey, identity);
    setMessages(rows => mergeChatMessages(rows, pair));
    if (currentText.current === entry.submittedText) changeText("");
    clearPending(entry); setHistoryFailed(false);
    if (historyFailed) setAttempt(value => value + 1);
  }
  useEffect(() => {
    if (!session.current || sending.current) return undefined;
    const controller = new AbortController(), token = gate.issue("history", scope), requestedSession = session.current;
    setLoading(true); setHistoryFailed(false);
    api.listChatMessages({ sessionId: requestedSession, limit: 100, signal: controller.signal }).then(result => {
      if (!isCurrent(token)) return;
      if (result.session?.session_id !== requestedSession || result.session?.project_id !== (projectId || "default")) {
        if (pending.current) setHistoryFailed(true); else forgetSession(requestedSession);
        setLoading(false); return;
      }
      setMessages(mergeChatMessages([], result.items || [])); setLoading(false);
    }).catch(reason => {
      if (!isCurrent(token)) return;
      if (reason?.kind === "empty" && !pending.current) forgetSession(requestedSession);
      else setHistoryFailed(true);
      setLoading(false);
    });
    return () => controller.abort();
  }, [api, scope, attempt, cacheKey]);
  useEffect(() => {
    const changed = () => {
      if (cacheKey === `${userStorageKey("companion-chat")}:${projectId || "default"}`) return;
      gate.invalidate("send"); gate.invalidate("history"); writer.current?.abort(); clearPending(pending.current);
    };
    window.addEventListener("chriptmas-user-space", changed);
    window.addEventListener("chriptmas-device-required", changed);
    return () => {
      window.removeEventListener("chriptmas-user-space", changed);
      window.removeEventListener("chriptmas-device-required", changed);
    };
  }, [cacheKey, projectId, scope]);
  useEffect(() => () => writer.current?.abort(), []);
  function cancelRetry() {
    if (sending.current) return;
    clearPending(pending.current); setFailed(false);
  }
  async function send(event, recover = false) {
    event?.preventDefault();
    if (sending.current || loading || (recover ? !pending.current : pending.current || historyFailed || !text.trim())) return;
    const token = gate.issue("send", scope); if (!token) return;
    const controller = new AbortController(); writer.current = controller;
    const entry = recover ? pending.current : { request: { requestId: globalThis.crypto.randomUUID(), text: text.trim(), projectId, sessionId: session.current,
      memoryId: text.trim() === initialPrompt.trim() && initialMemoryId ? initialMemoryId : null }, submittedText: text, draft: text };
    // 请求与原会话先保存，后续正文编辑只更新同一记录中的草稿。
    pending.current = entry; writeStoredJson(recoveryKey, entry, storage);
    gate.invalidate("history"); setLoading(false); sending.current = true; setBusy(true); setFailed(false);
    try {
      const request = entry.request;
      if (recover && request.sessionId !== null) {
        // 既有会话先核对已完成消息，避免再次提交已经被服务端接受的请求。
        const history = await api.listChatMessages({ sessionId: request.sessionId, limit: 100, signal: controller.signal });
        if (!isCurrent(token)) return;
        if (history.session?.session_id !== request.sessionId || history.session?.project_id !== (request.projectId || "default")) {
          throw new Error("chat_response_mismatch");
        }
        const pair = (history.items || []).filter(message => message.request_id === request.requestId)
          .sort((left, right) => Number(left.role === "assistant") - Number(right.role === "assistant"));
        if (pair.length) { acceptMessages(entry, history.session, pair); return; }
      }
      const result = await api.sendChat({ ...request, signal: controller.signal });
      if (!isCurrent(token)) return;
      acceptMessages(entry, result.session, [result.user_message, result.assistant_message]);
    }
    catch (failure) { if (isCurrent(token)) {
      // 明确拒绝或终态失败释放请求，未确认的网络与服务失败继续保留身份。
      if (failure.status === 400 && failure.code === "invalid_chat") clearPending(entry);
      setFailed(true);
    } }
    finally { if (writer.current === controller) writer.current = null; sending.current = false; if (isCurrent(token)) setBusy(false); }
  }
  return <section className="companion-page-chat" aria-label="聊聊" aria-busy={loading}><div className="companion-page-messages" aria-live="polite">{messages.map(message => <article key={message.message_id} data-role={message.role}><MarkdownBody>{message.content}</MarkdownBody></article>)}</div>{historyFailed && <Retry label="重试读取对话" onClick={() => { if (!sending.current) setAttempt(value => value + 1); }}/>}<form onSubmit={send}><textarea aria-label="和小熊说" rows={2} placeholder="说点什么" value={text} maxLength={4000} onChange={e => changeText(e.target.value)} onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !composing.current && !event.nativeEvent.isComposing && event.keyCode !== 229) { event.preventDefault(); send(); } }}/><button type="submit" aria-label="发送" disabled={busy || loading || historyFailed || Boolean(pending.current) || !text.trim()}><Icon name="send" size={18}/></button>{failed && <Retry label={pending.current ? "重试未确认消息" : "重试"} onClick={() => send(undefined, Boolean(pending.current))}/>} {failed && pending.current && <button type="button" className="companion-page-retry" onClick={cancelRetry}>取消重试</button>}</form></section>;
}
function Focus({ api }) {
  const [session, setSession] = useState(null), [loaded, setLoaded] = useState(false), [busy, setBusy] = useState(false), [failed, setFailed] = useState(false);
  const pending = useRef(false), failedAction = useRef(null), gate = useRequestScope(api), scope = gate.scope;
  useEffect(() => { const c = new AbortController(); let active = true;
    const token = gate.issue("session", scope);
    api.getFocus({ signal: c.signal }).then(value => { if (active && gate.isCurrent(token)) { setSession(value.session); setLoaded(true); } }).catch(() => { if (active && gate.isCurrent(token)) setFailed(true); });
    return () => { active = false; c.abort(); };
  }, [api, scope]);
  useEffect(() => {
    if (session?.status !== "running") return undefined;
    const c = new AbortController(); let active = true, polling = false;
    const timer = setInterval(() => {
      if (pending.current || polling) return;
      const token = gate.issue("session", scope);
      polling = true;
      const desktop = typeof globalThis.electronAPI?.backendBaseUrl === "string";
      (desktop ? api.getFocus({ signal: c.signal }) : api.observeFocus({ signal: c.signal })).then(value => {
        if (active && gate.isCurrent(token)) setSession(value.session);
      }).catch(() => { if (active && gate.isCurrent(token)) setFailed(true); }).finally(() => { polling = false; });
    }, 4000);
    return () => { active = false; clearInterval(timer); c.abort(); };
  }, [api, scope, session?.status]);
  async function perform(current, action, refresh = false) {
    if (pending.current) return;
    const token = gate.issue("session", scope); if (!token) return;
    const targetSessionId = current?.session_id ?? null;
    pending.current = true; setBusy(true); setFailed(false);
    try {
      if (refresh) {
        const value = await api.getFocus();
        if (!gate.isCurrent(token)) return;
        current = value.session; setSession(current); setLoaded(true);
      }
      const sameSession = (current?.session_id ?? null) === targetSessionId;
      const needsAction = sameSession && (action === "start" ? !["running", "paused"].includes(current?.status)
        : action === "pause" && current?.status === "running" || action === "resume" && current?.status === "paused");
      if (action && (!refresh || needsAction)) {
        const value = action === "start"
          ? await api.startFocus({ durationMinutes: 25, supervisionEnabled: false, workProcesses: [], distractingProcesses: [] })
          : await api.actOnFocus({ sessionId: current.session_id, action, expectedRevision: current.revision });
        if (!gate.isCurrent(token)) return;
        setSession(value.session); setLoaded(true);
      }
      if (gate.isCurrent(token)) failedAction.current = null;
    } catch { if (gate.isCurrent(token)) { failedAction.current = action ? { session: current, action } : null; setFailed(true); } }
    finally { pending.current = false; if (gate.isCurrent(token)) setBusy(false); }
  }
  function retry() { return perform(failedAction.current?.session, failedAction.current?.action, true); }
  const number = Number(session?.remaining_seconds ?? 1500), seconds = Number.isFinite(number) ? Math.max(0, number) : 1500;
  const action = session?.status === "running" ? "pause" : session?.status === "paused" ? "resume" : "start";
  return <section className="companion-page-focus" aria-label="专注时钟"><time>{String(Math.floor(seconds / 60)).padStart(2, "0")}:{String(Math.floor(seconds % 60)).padStart(2, "0")}</time><button type="button" disabled={busy || !loaded} onClick={() => perform(session, action)}>{action === "pause" ? "暂停" : action === "resume" ? "继续" : "开始"}</button>{failed && <Retry onClick={retry}/>}</section>;
}
