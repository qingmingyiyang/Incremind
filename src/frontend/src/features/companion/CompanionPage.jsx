import { useEffect, useMemo, useRef, useState } from "react";
import { Icon, MarkdownBody, StatusDot } from "../../shared/ui";
import { createCompanionApi } from "./companionApi";
import "./companionPage.css";
const MODES = [["chat", "聊聊"], ["review", "回顾"], ["focus", "专注"], ["plan", "安排"]];
const LINES = { chat: "在呢。", review: "看看这一周。", focus: "我陪你专注一会儿。", plan: "从记住的事开始。" };
export function CompanionPage({ projectId = "", api: injectedApi, initialMode = "chat", initialPrompt = "", initialMemoryId = "" }) {
  const api = useMemo(() => injectedApi || createCompanionApi(), [injectedApi]);
  const [mode, setMode] = useState(initialMode);
  return <main className="companion-page" aria-label="伙伴">
    <aside className="companion-page-bear"><div className="companion-page-sprite" data-mode={mode} role="img" aria-label="小熊"/><p>{LINES[mode]}</p>{typeof globalThis.electronAPI?.enterCompanionMode === "function" && <button type="button" className="companion-page-pet" onClick={() => globalThis.electronAPI.enterCompanionMode()}>显示桌宠</button>}</aside>
    <section className="companion-page-content"><nav className="companion-page-modes" aria-label="伙伴模式">{MODES.map(([key, label]) => <button key={key} type="button" aria-pressed={mode === key} onClick={() => setMode(key)}>{label}</button>)}</nav>
    <div className="companion-page-mode" hidden={mode !== "chat"} key={`chat:${projectId}`}><Chat api={api} projectId={projectId} initialPrompt={initialPrompt} initialMemoryId={initialMemoryId}/></div>
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
function Chat({ api, projectId, initialPrompt, initialMemoryId }) {
  const [text, setText] = useState(initialPrompt), [messages, setMessages] = useState([]), [busy, setBusy] = useState(false), [failed, setFailed] = useState(false);
  const live = useLive(), session = useRef(null), pending = useRef(null), sending = useRef(false);
  async function send(event) {
    event?.preventDefault(); const value = text.trim(); if (!value || sending.current) return;
    const request = pending.current?.text === value ? pending.current : { requestId: globalThis.crypto.randomUUID(), text: value, projectId, ...(value === initialPrompt.trim() && initialMemoryId ? { memoryId: initialMemoryId } : {}) };
    pending.current = request; sending.current = true; setBusy(true); setFailed(false);
    try { const result = await api.sendChat({ ...request, sessionId: session.current }); if (live.current) { session.current = result.session?.session_id || session.current; setMessages(rows => [...rows, result.user_message, result.assistant_message].filter(Boolean)); setText(""); pending.current = null; } }
    catch { if (live.current) setFailed(true); }
    finally { sending.current = false; if (live.current) setBusy(false); }
  }
  return <section className="companion-page-chat" aria-label="聊聊"><div className="companion-page-messages" aria-live="polite">{messages.map(message => <article key={message.message_id} data-role={message.role}><MarkdownBody>{message.content}</MarkdownBody></article>)}</div><form onSubmit={send}><input aria-label="和小熊说" placeholder="说点什么" value={text} maxLength={4000} onChange={e => setText(e.target.value)}/><button type="submit" aria-label="发送" disabled={busy || !text.trim()}><Icon name="send" size={18}/></button>{failed && <Retry onClick={() => send()}/>}</form></section>;
}
function Focus({ api }) {
  const [session, setSession] = useState(null), [loaded, setLoaded] = useState(false), [busy, setBusy] = useState(false), [failed, setFailed] = useState(false);
  const live = useLive(), pending = useRef(false), failedAction = useRef(null);
  useEffect(() => { const c = new AbortController(); let active = true;
    api.getFocus({ signal: c.signal }).then(value => { if (active) { setSession(value.session); setLoaded(true); } }).catch(() => { if (active) { failedAction.current = null; setFailed(true); } });
    return () => { active = false; c.abort(); };
  }, [api]);
  useEffect(() => {
    if (session?.status !== "running") return undefined;
    let active = true;
    const timer = setInterval(() => {
      const desktop = typeof globalThis.electronAPI?.backendBaseUrl === "string";
      (desktop ? api.getFocus() : api.observeFocus()).then(value => {
        if (active && !pending.current) setSession(current => !current || !value.session || value.session.revision >= current.revision ? value.session : current);
      }).catch(() => { if (active && !pending.current) { failedAction.current = null; setFailed(true); } });
    }, 4000);
    return () => { active = false; clearInterval(timer); };
  }, [api, session?.status]);
  async function perform(current, action) {
    if (pending.current) return; pending.current = true; setBusy(true); setFailed(false);
    try {
      const value = action === "start"
        ? await api.startFocus({ durationMinutes: 25, supervisionEnabled: false, workProcesses: [], distractingProcesses: [] })
        : await api.actOnFocus({ sessionId: current.session_id, action, expectedRevision: current.revision });
      if (live.current) { setSession(value.session); setLoaded(true); failedAction.current = null; }
    } catch { if (live.current) { failedAction.current = action; setFailed(true); } }
    finally { pending.current = false; if (live.current) setBusy(false); }
  }
  async function retry() {
    if (pending.current) return; pending.current = true; setBusy(true);
    const action = failedAction.current;
    try {
      const value = await api.getFocus();
      if (!live.current) return;
      setSession(value.session); setLoaded(true); setFailed(false); pending.current = false;
      if (action === "start" && !["running", "paused"].includes(value.session?.status)) await perform(value.session, action);
      else if (action === "pause" && value.session?.status === "running" || action === "resume" && value.session?.status === "paused") await perform(value.session, action);
      else failedAction.current = null;
    } catch { if (live.current) setFailed(true); }
    finally { pending.current = false; if (live.current) setBusy(false); }
  }
  const number = Number(session?.remaining_seconds ?? 1500), seconds = Number.isFinite(number) ? Math.max(0, number) : 1500;
  const action = session?.status === "running" ? "pause" : session?.status === "paused" ? "resume" : "start";
  return <section className="companion-page-focus" aria-label="专注时钟"><time>{String(Math.floor(seconds / 60)).padStart(2, "0")}:{String(Math.floor(seconds % 60)).padStart(2, "0")}</time><button type="button" disabled={busy || !loaded} onClick={() => perform(session, action)}>{action === "pause" ? "暂停" : action === "resume" ? "继续" : "开始"}</button>{failed && <Retry onClick={retry}/>}</section>;
}
