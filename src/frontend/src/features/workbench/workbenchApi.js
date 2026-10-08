import { productFetch as fetch, userStorageKey } from '../../shared/api/deviceTransport';
import { readResponseJson } from '../../shared/lib/responseJson';
import { libraryBackendUrl } from '../rebuild/libraryOverviewTransport';
import { readStoredJson, writeStoredJson, removeStoredItem } from '../../shared/lib/browserStorage';

const messages = {
  intent_not_ready: '此意图暂不可用 · 选择记住', project_tag_not_found: '项目未找到 · 修改标签',
  ambiguous_project_tag: '项目重名 · 使用项目编号', private_project_remote_blocked: '项目为私密 · 查看设置',
  remote_disabled: '模型外发已关闭 · 查看设置', turn_in_progress: '回答仍在生成 · 稍后重接',
  turn_interrupted: '回答已中断 · 重新发送', source_changed_retry: '资料已变化 · 重新发送',
  stream_disconnected: '连接已中断 · 重接回答', answer_generation_failed: '回答未完成 · 重试',
};
function failure(code) { const error = new Error(messages[code] || '操作未完成 · 重试'); error.code = code; return error; }
const pendingKeys = new Map();
function submission(body, action = '') {
  let storage; try { storage = globalThis.sessionStorage; } catch { storage = null; }
  const slot = userStorageKey(`chriptmas-v2-request:${body.project_id}${action ? ':' + action : ''}`), signature = JSON.stringify(body);
  const cached = pendingKeys.get(slot) || (storage ? readStoredJson(slot, storage).value : null);
  const value = cached?.body === signature ? cached : { body: signature, id: globalThis.crypto?.randomUUID?.() || `request-${Date.now()}-${Math.random().toString(36).slice(2)}` };
  pendingKeys.set(slot, value);
  if (storage) writeStoredJson(slot, value, storage);
  return { id: value.id, clear: () => {
    // 请求身份和槽位在提交时固定，迟到响应只清理自己的重试记录。
    if (pendingKeys.get(slot)?.id === value.id) pendingKeys.delete(slot);
    if (storage && readStoredJson(slot, storage).value?.id === value.id) removeStoredItem(slot, storage);
  } };
}
function aborted() { return new DOMException('Aborted', 'AbortError'); }
async function streamFetch(...args) {
  try { return await fetch(...args); }
  catch (error) { if (error instanceof TypeError) throw failure('stream_disconnected'); throw error; }
}
function readState(project, turnId = null, after = 0) {
  if (!Number.isSafeInteger(after) || after < 0) throw failure('invalid_response');
  return { project, turnId, after, acceptedHeadless: false, consumerFailed: false };
}
function waitForRead(delay, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(aborted()); return; }
    let timer = null;
    const cleanup = () => { clearTimeout(timer); globalThis.window?.removeEventListener('online', online); signal?.removeEventListener('abort', abort); };
    const ready = () => { cleanup(); resolve(); };
    const abort = () => { cleanup(); reject(aborted()); };
    const online = () => { if (globalThis.navigator?.onLine !== false) ready(); };
    globalThis.window?.addEventListener('online', online);
    signal?.addEventListener('abort', abort, { once: true });
    if (globalThis.navigator?.onLine !== false) {
      if (delay === 0) ready();
      else timer = setTimeout(ready, delay);
    }
  });
}
async function readStream(response, { onStarted, onDelta, onReset, onStatus, onCursor, signal }, state) {
  const reader = response.body?.getReader();
  if (!reader) throw failure('stream_disconnected');
  const decoder = new TextDecoder(); let buffer = '';
  let pending = '', frame = null, received = state.after, rejectRead = null;
  let consumerError;
  const observe = (callback, value) => {
    try { callback?.(value); }
    catch (error) { state.consumerFailed = true; consumerError = error; throw error; }
  };
  const acknowledge = sequence => { observe(onCursor, sequence); state.after = sequence; };
  const cancelFrame = () => {
    if (frame !== null) {
      if (typeof globalThis.cancelAnimationFrame === 'function') globalThis.cancelAnimationFrame(frame);
      else clearTimeout(frame);
      frame = null;
    }
  };
  const flush = () => {
    cancelFrame(); const text = pending;
    if (text && !signal?.aborted) {
      observe(onDelta, text); pending = ''; acknowledge(received);
    }
  };
  const queue = text => {
    if (!onDelta || signal?.aborted) return;
    pending += text;
    const display = () => {
      try { flush(); }
      catch (error) { rejectRead?.(error); }
    };
    if (frame === null) frame = typeof globalThis.requestAnimationFrame === 'function'
      ? globalThis.requestAnimationFrame(display) : setTimeout(display, 16);
  };
  const dispose = () => { cancelFrame(); pending = ''; };
  const abort = () => { dispose(); reader.cancel().catch(() => {}); };
  signal?.addEventListener('abort', abort, { once: true });
  try {
    while (true) {
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
      if (state.consumerFailed) throw consumerError;
      let done, value;
      try {
        ({ done, value } = await new Promise((resolve, reject) => {
          rejectRead = reject; reader.read().then(resolve, reject);
        }));
      } catch (error) {
        if (state.consumerFailed) throw consumerError;
        if (error instanceof TypeError) throw failure('stream_disconnected'); throw error;
      } finally { rejectRead = null; }
      if (signal?.aborted) throw aborted();
      buffer += decoder.decode(value, { stream: !done });
      if (buffer.length > 2_000_000) throw failure('stream_disconnected');
      let boundary;
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
        const frame = buffer.slice(0, boundary.index); buffer = buffer.slice(boundary.index + boundary[0].length);
        let event = 'message', id = null; const data = [];
        for (const line of frame.split(/\r?\n/)) {
          if (line.startsWith('event:')) event = line.slice(6).trim();
          if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
          if (line.startsWith('id:')) {
            const raw = line.slice(3).trim();
            if (!/^[1-9]\d*$/.test(raw) || !Number.isSafeInteger(Number(raw))) throw failure('invalid_response');
            id = Number(raw);
          }
        }
        if (!data.length) continue;
        if (id !== null && id <= received && event !== 'done') continue;
        let payload;
        try { payload = JSON.parse(data.join('\n')); }
        catch { throw failure('invalid_response'); }
        if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw failure('invalid_response');
        if (event === 'started') {
          if (typeof payload.turn?.id !== 'string' || !payload.turn.id || state.turnId && payload.turn.id !== state.turnId) throw failure('invalid_response');
          state.turnId = payload.turn.id;
          if (id === null && state.after === 0) state.acceptedHeadless = true;
          observe(onStarted, payload);
        }
        else if (event === 'delta' && typeof payload.text === 'string' && payload.part === undefined) queue(payload.text);
        else if (event === 'reset') { flush(); observe(onReset, payload); }
        else if (event === 'status') { flush(); observe(onStatus, payload); }
        else if (event === 'done') {
          if (state.turnId && payload.turn?.id !== state.turnId) throw failure('invalid_response');
          flush(); return payload;
        }
        else if (event === 'error') throw failure(payload.code);
        if (id !== null) {
          received = id;
          state.acceptedHeadless = false;
          // A persisted UI cursor may only acknowledge already displayed text.
          if (!pending) acknowledge(id);
        }
      }
      if (done) throw failure('stream_disconnected');
    }
  } finally {
    try { if (!signal?.aborted && !state.consumerFailed) { flush(); observe(onCursor, state.after); } }
    finally {
      dispose();
      signal?.removeEventListener('abort', abort);
      await reader.cancel().catch(() => {}); reader.releaseLock();
    }
  }
}
function retryableRead(error, state) {
  return !state.consumerFailed && ['stream_disconnected', 'server_unavailable'].includes(error.code);
}
async function resumeReads(state, options, initialDelay = 0) {
  let delay = initialDelay;
  while (true) {
    await waitForRead(delay, options.signal);
    if (options.signal?.aborted) throw aborted();
    if (globalThis.navigator?.onLine === false) continue;
    try {
      const path = `/api/v2/workbench/turns/${encodeURIComponent(state.turnId)}/stream?${new URLSearchParams({ project_id: state.project, after: String(state.after) })}`;
      const response = await streamFetch(libraryBackendUrl(path), { method: 'GET', cache: 'no-store', signal: options.signal,
        headers: { Accept: 'text/event-stream', 'Last-Event-ID': String(state.after) } });
      if (!response.ok) {
        if (response.status >= 500) throw failure('server_unavailable');
        // Only the original POST's accepted, not-yet-persisted binding may
        // wait for a head. An unknown refresh id still fails closed on 404.
        if (response.status === 404 && state.acceptedHeadless) throw failure('stream_disconnected');
        throw failure((await readResponseJson(response)).detail);
      }
      if (!response.headers?.get('content-type')?.startsWith('text/event-stream')) throw failure('invalid_response');
      return await readStream(response, options, state);
    } catch (error) {
      if (options.signal?.aborted) throw aborted();
      if (!retryableRead(error, state)) throw error;
      delay = Math.min(delay ? delay * 2 : 1000, 30000);
    }
  }
}
async function createTurn(body, options = {}) {
  // 不带意图时由后端路由判断：单个记住返回 JSON，问、干活和多部分返回 SSE。
  if (body.intent && !['ask', 'do'].includes(body.intent)) return post('/api/v2/workbench/turns', body);
  const key = submission(body);
  const state = readState(body.project_id);
  try {
    const response = await streamFetch(libraryBackendUrl('/api/v2/workbench/turns'), { method: 'POST', cache: 'no-store',
      signal: options.signal, headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream, application/json;q=0.5',
        'Idempotency-Key': key.id }, body: JSON.stringify(body) });
    if (!response.ok) throw failure((await readResponseJson(response)).detail);
    let value;
    if (response.headers?.get('content-type')?.startsWith('text/event-stream')) {
      try { value = await readStream(response, options, state); }
      catch (error) {
        if (!state.turnId || options.signal?.aborted || !retryableRead(error, state)) throw error;
        value = await resumeReads(state, options, 1000);
      }
    } else value = await readResponseJson(response);
    key.clear(); return value;
  } catch (error) {
    if (!state.consumerFailed && error.code && !['turn_in_progress', 'stream_disconnected', 'server_unavailable', 'invalid_response'].includes(error.code)) key.clear();
    throw error;
  }
}
async function continueTurn(project, turnId, options = {}) {
  if (options.signal?.aborted) throw aborted();
  if (typeof turnId !== 'string' || !turnId) throw failure('invalid_response');
  const body = { project_id: project };
  const key = submission(body, `continue:${turnId}`);
  try {
    const response = await streamFetch(libraryBackendUrl(`/api/v2/workbench/turns/${encodeURIComponent(turnId)}/continue`), {
      method: 'POST', cache: 'no-store', signal: options.signal,
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key.id }, body: JSON.stringify(body),
    });
    const value = await readResponseJson(response);
    if (options.signal?.aborted) throw aborted();
    if (!response.ok) throw failure(value.detail);
    if (value?.id !== turnId || !['ask', 'do'].includes(value.intent)
        || !value.receipt || typeof value.receipt !== 'object' || Array.isArray(value.receipt)) throw failure('invalid_response');
    key.clear(); return value;
  } catch (error) {
    if (error.name !== 'AbortError' && error.code
        && !['turn_in_progress', 'stream_disconnected', 'server_unavailable', 'invalid_response'].includes(error.code)) key.clear();
    throw error;
  }
}
async function request(path, options = {}) {
  const response = await fetch(libraryBackendUrl(path), { cache: 'no-store', ...options });
  const value = await readResponseJson(response);
  if (!response.ok) {
    const code = value.detail;
    throw failure(code);
  }
  return value;
}
const post = (path, body) => request(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
export const workbenchApi = {
  createProject: name => post('/api/v2/projects',{name}),
  threads: project => request(`/api/v2/workbench/threads?${new URLSearchParams({ project_id: project })}`),
  thread: (project, id) => request(`/api/v2/workbench/threads/${encodeURIComponent(id)}?${new URLSearchParams({ project_id: project })}`),
  create: createTurn,
  continue: continueTurn,
  resume: async (project, turnId, options = {}) => {
    if (typeof turnId !== 'string' || !turnId) throw failure('invalid_response');
    return resumeReads(readState(project, turnId, options.after ?? 0), options);
  },
  division: (project,id) => request(`/api/v2/workbench/turns/${encodeURIComponent(id)}/division?${new URLSearchParams({project_id:project})}`),
  saveDivision: (project,id,items,revision) => request(`/api/v2/workbench/turns/${encodeURIComponent(id)}/division`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_id:project,items,expected_revision:revision})}),
  deleteDivision: (project,id,revision) => request(`/api/v2/workbench/turns/${encodeURIComponent(id)}/division`,{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_id:project,expected_revision:revision})}),
  redo: (project,id,revision,options={}) => post(`/api/v2/workbench/turns/${encodeURIComponent(id)}/redo`,{project_id:project,expected_revision:revision,
    ...(Object.hasOwn(options, 'continue_from') ? { continue_from: options.continue_from } : {})}),
  retry: (project, id) => post(`/api/v2/workbench/turns/${encodeURIComponent(id)}/retry`, { project_id: project }),
  insight: (project, insight, action) => post(`/api/v2/library/insights/${encodeURIComponent(insight.id)}/${action}`, { project_id: project, expected_revision: insight.revision }),
  file: (project, file, files = []) => { const body = new FormData(); body.append('project_id', project); body.append('file', file); files.forEach(image => body.append('files', image)); return request('/api/v2/workbench/files', { method: 'POST', body }); },
  images: (project, itemId) => request(`/api/v2/workbench/items/${encodeURIComponent(itemId)}/images?project_id=${encodeURIComponent(project)}`),
};
