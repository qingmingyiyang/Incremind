import { productFetch as fetch, readDeviceCredential, userStorageKey } from '../../shared/api/deviceTransport';
import { readResponseJson } from '../../shared/lib/responseJson';
import { libraryBackendUrl } from '../rebuild/libraryOverviewTransport';
import { readStoredJson, writeStoredJson, removeStoredItem } from '../../shared/lib/browserStorage';
import { remoteRestriction } from '../../shared/lib/privacyMessages';
import { readExternalProgressFrame } from './externalProgress';
import { threadHistoryQuery, readThreadHistory, readScopedTurn } from '../../shared/lib/threadHistory';

const messages = {
  shared_intent_not_available: '共享项目当前可提问', shared_scope_tag_unsupported: '请从顶部选择共享项目',
  intent_not_ready: '此意图暂不可用 · 选择记住', project_tag_not_found: '项目未找到 · 修改标签',
  ambiguous_project_tag: '项目重名 · 使用项目编号', turn_in_progress: '回答仍在生成 · 稍后重接',
  turn_interrupted: '回答已中断 · 重新发送', source_changed_retry: '资料已变化 · 重新发送',
  stream_disconnected: '连接已中断 · 重接回答', answer_generation_failed: '回答未完成 · 重试',
  interrupted: '处理已中断 · 重试',
  task_execution_failed: '任务执行失败',
  task_progress_failed: '任务进度未能保存',
  task_outcome_identity_changed: '原成果身份已变化',
  task_outcome_not_found: '原成果不可用',
  model_not_configured: '模型未配置',
  outcome_selection_changed: '成果版本已变化',
  task_division_revision_conflicted: '分工或原任务已变化 · 重新读取后重试',
  task_division_not_found: '分工样例已删除',
  turn_not_retryable: '原任务暂不可重试 · 查看执行状态',
  dependency_failed: '原件未完成 · 重试原件后重新发送',
  dependency_timeout: '原件仍在处理 · 完成后重新发送',
  storage_quota_exceeded: '空间已满 · 联系管理员调整配额',
  job_quota_exceeded: '今日任务额度已用完 · 次日重试',
  server_model_balance_insufficient: '余额不足 · 联系管理员',
  server_model_balance_account_blocked: '消费待核算 · 联系管理员',
  server_model_balance_bound_unavailable: '该模型不支持共享计费 · 联系管理员',
  server_model_balance_price_unavailable: '模型价格未配置 · 联系管理员',
  server_model_balance_model_forbidden: '模型未获授权 · 联系管理员',
  server_model_binding_changed: '模型分配已变化 · 重新发送',
  server_model_purpose_unavailable: '尚未分配模型 · 联系管理员',
  server_model_profile_disabled: '模型已停用 · 联系管理员',
  server_model_credential_unavailable: '模型密钥不可用 · 联系管理员',
  server_model_secret_unavailable: '模型密钥不可用 · 联系管理员',
  server_model_not_allowed: '模型未获授权 · 联系管理员',
  server_model_balance_job_unauthorized: '调用权限不可用 · 联系管理员',
  server_model_billing_owner_unavailable: '计费状态不可用 · 联系管理员',
  server_model_billing_unavailable: '计费状态不可用 · 联系管理员',
  text_file_encoding_invalid: '请将文件保存为 UTF-8',
  document_password_required: '文件需要密码 · 解密后上传',
  document_text_empty: '文件没有可提取文字 · 换用文字文件',
  document_invalid: '文件损坏或格式不符 · 重新导出后上传',
  invalid_asr_lexicon_changes: '热词变更格式无效 · 重新提出修改',
  asr_lexicon_revision_conflict: '热词表已变化 · 重新读取并预览',
  asr_lexicon_change_conflict: '同一批次重复修改词语 · 分开提出修改',
  asr_lexicon_term_exists: '目标词语已存在 · 核对后重新提出修改',
  asr_lexicon_term_not_found: '词语未找到 · 重新读取词表',
  asr_lexicon_term_retired: '词语已停用 · 先恢复词语',
  asr_lexicon_capacity_exceeded: '热词表已达数量上限 · 停用不需要的词语',
  agent_data_preview_changed: '变更预览已变化 · 重新提出修改',
  agent_data_read_revision_changed: '热词表已变化 · 重新读取并预览',
  asr_lexicon_update_not_completed: '热词变更未保存 · 重新读取词表',
  asr_lexicon_read_not_completed: '热词表未读取 · 重新提出查询',
};
export function workbenchErrorMessage(value, fallback = '操作未完成 · 重试') {
  const code = typeof value === 'string' ? value : value?.code;
  if (typeof code === 'string' && code.startsWith('external_')) return '外部执行未完成 · 重试';
  if (typeof code === 'string' && code.startsWith('server_model_'))
    return Object.hasOwn(messages, code) ? messages[code] : fallback;
  return remoteRestriction(value)?.message || (Object.hasOwn(messages, code) ? messages[code] : null) || value?.message || fallback;
}
function failure(code) { const error = new Error(workbenchErrorMessage(code)); error.code = code; return error; }
function finishedFailure(error, project, turnId) {
  if (turnId && error.name !== 'AbortError' && typeof error.code === 'string' && error.code
      && !['turn_in_progress', 'stream_disconnected', 'server_unavailable', 'invalid_response', 'thread_scope_changed'].includes(error.code))
    error.finishedTurn = { project, id: turnId };
  return error;
}
const pendingKeys = new Map();
function submissionSlot(project, scopeKind, action) {
  threadHistoryQuery(project, scopeKind);
  let storage; try { storage = globalThis.sessionStorage; } catch { storage = null; }
  const projectSlot = scopeKind === 'shared' ? `shared:${project}` : project;
  return { storage, slot: userStorageKey(`chriptmas-v2-request:${projectSlot}${action ? ':' + action : ''}`) };
}
function submission(body, action = '', { durable = false } = {}) {
  const { storage, slot } = submissionSlot(body.project_id, body.scope_kind ?? 'personal', action);
  const signature = JSON.stringify(body);
  const cached = pendingKeys.get(slot) || (storage ? readStoredJson(slot, storage).value : null);
  const value = cached?.body === signature ? cached : { body: signature, id: globalThis.crypto?.randomUUID?.() || `request-${Date.now()}-${Math.random().toString(36).slice(2)}` };
  if (durable && (!storage || !writeStoredJson(slot, value, storage))) throw new Error('重做编号未保存 · 核对浏览器存储');
  pendingKeys.set(slot, value);
  if (!durable && storage) writeStoredJson(slot, value, storage);
  return { id: value.id, clear: () => {
    // 请求身份和槽位在提交时固定，迟到响应只清理自己的重试记录。
    if (pendingKeys.get(slot)?.id === value.id) pendingKeys.delete(slot);
    if (storage && readStoredJson(slot, storage).value?.id === value.id) removeStoredItem(slot, storage);
  } };
}
function sharedRedoAction(turnId) {
  const device = readDeviceCredential()?.device?.device_id;
  if (!device) throw new Error('共享重做设备未核对');
  return `redo:${turnId}:device:${encodeURIComponent(device)}`;
}
function pendingSharedRedo(project, turnId) {
  const { storage, slot } = submissionSlot(project, 'shared', sharedRedoAction(turnId));
  const record = readStoredJson(slot, storage);
  if (record.unavailable || record.invalid) throw new Error('重做编号未读取 · 核对浏览器存储');
  if (!record.value) return null;
  let body;
  try { body = JSON.parse(record.value.body); } catch { throw new Error('原重做编号未核对'); }
  if (typeof record.value.id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(record.value.id) || body?.project_id !== project
      || body.scope_kind !== 'shared' || body.source_kind !== 'execution'
      || !Number.isSafeInteger(body.expected_revision) || body.expected_revision < 1
      || Object.keys(body).length !== 4) throw new Error('原重做编号未核对');
  const wireBody = { project_id: project, expected_revision: body.expected_revision, source_kind: body.source_kind };
  return { id: record.value.id, project, turnId, slot, body: wireBody, signature: record.value.body };
}
function currentSharedRedo(command) {
  const saved = pendingSharedRedo(command.project, command.turnId);
  if (!saved || saved.id !== command.id || saved.slot !== command.slot || saved.signature !== command.signature)
    throw new Error('原重做编号已变化 · 核对');
  return saved;
}
function prepareSharedRedo(project, turnId, revision) {
  if (pendingSharedRedo(project, turnId)) throw new Error('请先核对原重做编号');
  submission({ project_id: project, expected_revision: revision, source_kind: 'execution', scope_kind: 'shared' },
    sharedRedoAction(turnId), { durable: true });
  return pendingSharedRedo(project, turnId);
}
function settleSharedRedo(command) {
  const saved = currentSharedRedo(command);
  const { storage } = submissionSlot(saved.project, 'shared', sharedRedoAction(saved.turnId));
  if (!removeStoredItem(saved.slot, storage)) throw new Error('原重做编号未清理 · 核对浏览器存储');
  if (pendingKeys.get(saved.slot)?.id === saved.id) pendingKeys.delete(saved.slot);
}
function readSharedTaskResult(value, project, threadId) {
  readThreadHistory({ id: value?.thread_id, scope_kind: 'shared', turns: [value?.turn] }, threadId ?? value?.thread_id, project, 'shared');
  const task = value.turn.receipt.do;
  if (value.turn.intent !== 'do' || !task || typeof task !== 'object' || Array.isArray(task)) throw failure('invalid_response');
}
async function checkSharedRedo(command, threadId, isCurrent, { signal } = {}) {
  const { id, project, turnId, body } = currentSharedRedo(command);
  const params = new URLSearchParams({ project_id: project, scope_kind: 'shared', redo_from: turnId,
    expected_revision: String(body.expected_revision) });
  let value;
  try { value = await request(`/api/v2/workbench/task-commands/${encodeURIComponent(id)}?${params}`, { signal }); }
  catch (error) {
    if (isCurrent() && error.status === 404 && error.code === 'shared_task_not_received') return { state: 'not_received' };
    throw error;
  }
  if (!isCurrent()) return null;
  if (value?.command_id !== id || value.project_id !== project || value.scope_kind !== 'shared'
      || value.redo_from !== turnId || value.expected_revision !== body.expected_revision
      || !['accepted', 'failed', 'preparing', 'unknown'].includes(value.state)) throw failure('invalid_response');
  if (value.state === 'failed' && (typeof value.error !== 'string' || !value.error)) throw failure('invalid_response');
  if (value.state !== 'accepted') return { state: value.state, error: value.error };
  if (value.thread_id !== threadId || typeof value.turn_id !== 'string'
      || !/^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/.test(value.turn_id) || value.turn_id === turnId) throw failure('invalid_response');
  const detail = await workbenchApi.thread(project, threadId, { scopeKind: 'shared', signal });
  if (!isCurrent()) return null;
  const turn = detail.turns.find(item => item.id === value.turn_id);
  const result = { thread_id: threadId, turn };
  readSharedTaskResult(result, project, threadId);
  if (turn.thread_id !== threadId) throw failure('invalid_response');
  return { state: 'accepted', result };
}
async function submitSharedRedo(command, threadId) {
  const { id, project, turnId, body } = currentSharedRedo(command);
  const result = await request(`/api/v2/workbench/turns/${encodeURIComponent(turnId)}/redo`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': id }, body: JSON.stringify(body),
  });
  readSharedTaskResult(result, project, threadId);
  if (result.turn.id === turnId || result.turn.thread_id !== threadId) throw failure('invalid_response');
  return result;
}
export { submission as workbenchSubmission };
function aborted() { return new DOMException('Aborted', 'AbortError'); }
async function streamFetch(...args) {
  try { return await fetch(...args); }
  catch (error) { if (error instanceof TypeError) throw failure('stream_disconnected'); throw error; }
}
function readState(project, turnId = null, after = 0, scopeKind = 'personal') {
  if (!Number.isSafeInteger(after) || after < 0) throw failure('invalid_response');
  threadHistoryQuery(project, scopeKind);
  return { project, turnId, scopeKind, turnIntent: null, after, acceptedHeadless: false, consumerFailed: false };
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
async function readStream(response, { onStarted, onDelta, onPartDelta, onReset, onSteer, onStatus, onTaskProgress, onCursor, signal }, state) {
  const reader = response.body?.getReader();
  if (!reader) throw failure('stream_disconnected');
  const decoder = new TextDecoder(); let buffer = '';
  let pending = '', pendingPart = null, frame = null, received = state.after, rejectRead = null;
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
      if (pendingPart === null) observe(onDelta, text);
      else observe(onPartDelta, { part: pendingPart, text });
      pending = ''; pendingPart = null; acknowledge(received);
    }
  };
  const queue = (text, part = null) => {
    if (!(part === null ? onDelta : onPartDelta) || signal?.aborted) return;
    if (pending && pendingPart !== part) flush();
    pendingPart = part; pending += text;
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
          readScopedTurn(payload.turn, state.scopeKind);
          if (typeof payload.turn?.id !== 'string' || !payload.turn.id || state.turnId && payload.turn.id !== state.turnId) throw failure('invalid_response');
          state.turnId = payload.turn.id;
          state.turnIntent = payload.turn.intent;
          if (id === null && state.after === 0) state.acceptedHeadless = true;
          observe(onStarted, payload);
        }
        else if (event === 'delta' && typeof payload.text === 'string') {
          if (payload.part == null) queue(payload.text);
          else if (Number.isSafeInteger(payload.part) && payload.part >= 0) queue(payload.text, payload.part);
          else throw failure('invalid_response');
        }
        else if (event === 'reset') { flush(); observe(onReset, payload); }
        else if (event === 'steer') { flush(); observe(onSteer, payload); }
        else if (event === 'status') { flush(); observe(onStatus, payload); }
        else if (event === 'task_progress') { flush(); observe(onTaskProgress, readExternalProgressFrame(payload, state)); }
        else if (event === 'done') {
          readScopedTurn(payload.turn, state.scopeKind);
          if (state.turnId && payload.turn?.id !== state.turnId) throw failure('invalid_response');
          flush(); return payload;
        }
        else if (event === 'error') throw finishedFailure(failure(payload.code), state.project, state.turnIntent ? state.turnId : null);
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
  const key = options.submissionKey ? { id: options.submissionKey, clear: () => options.onSubmissionSettled?.() } : submission(body);
  const state = readState(body.project_id, null, 0, body.scope_kind || 'personal');
  if (body.executor && body.executor !== 'builtin') state.externalExecutor = body.executor;
  try {
    const response = await streamFetch(libraryBackendUrl('/api/v2/workbench/turns'), { method: 'POST', cache: 'no-store',
      signal: options.signal, headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream, application/json;q=0.5',
        'Idempotency-Key': key.id }, body: JSON.stringify(body) });
    if (!response.ok) {
      const error = failure((await readResponseJson(response)).detail); error.status = response.status; throw error;
    }
    let value;
    if (response.headers?.get('content-type')?.startsWith('text/event-stream')) {
      try { value = await readStream(response, options, state); }
      catch (error) {
        if (!state.turnId || options.signal?.aborted || !retryableRead(error, state)) throw error;
        value = await resumeReads(state, options, 1000);
      }
    } else value = await readResponseJson(response);
    readScopedTurn(value?.turn, state.scopeKind);
    if (state.scopeKind === 'shared' && body.intent === 'do') readSharedTaskResult(value, body.project_id, body.thread_id);
    key.clear(); return value;
  } catch (error) {
    if (error.name !== 'AbortError' && !state.consumerFailed && error.code && !['turn_in_progress', 'stream_disconnected', 'server_unavailable', 'invalid_response', 'thread_scope_changed'].includes(error.code)) {
      // 已接受的失败先由调用方回读，确认原任务后再释放本次请求身份。
      if (state.turnId && (state.turnIntent === 'do' || state.scopeKind === 'shared' && state.turnIntent === 'ask')) error.clearAcceptedRequest = key.clear;
      else key.clear();
    }
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
    if (!response.ok) throw response.status === 503 ? failure('server_unavailable')
      : finishedFailure(failure(value.detail), project, turnId);
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
    const error = failure(code); error.status = response.status; throw error;
  }
  return value;
}
const post = (path, body) => request(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
export const workbenchApi = {
  steerState: (project, id, { signal } = {}) => request(`/api/v2/workbench/turns/${encodeURIComponent(id)}/steer?${new URLSearchParams({ project_id: project })}`, { signal }),
  steer: (project, id, text, key, { signal } = {}) => request(`/api/v2/workbench/turns/${encodeURIComponent(id)}/steer`, {
    method: 'POST', signal, headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
    body: JSON.stringify({ project_id: project, text }),
  }),
  readSteer: (project, id, key, { signal } = {}) => request(`/api/v2/workbench/turns/${encodeURIComponent(id)}/steer?${new URLSearchParams({ project_id: project })}`, {
    signal, headers: { 'Idempotency-Key': key },
  }),
  createProject: name => post('/api/v2/projects',{name}),
  threads: (project, { scopeKind = 'personal', signal } = {}) => request(`/api/v2/workbench/threads?${threadHistoryQuery(project, scopeKind)}`, { signal }),
  thread: (project, id, { signal, scopeKind = 'personal' } = {}) => request(`/api/v2/workbench/threads/${encodeURIComponent(id)}?${threadHistoryQuery(project, scopeKind)}`, { signal })
    .then(value => readThreadHistory(value, id, project, scopeKind)),
  saveOutcome: (project, id, { signal } = {}) => request(`/api/v2/workbench/turns/${encodeURIComponent(id)}/save-outcome`, {
    method: 'POST', signal, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project_id: project }),
  }),
  create: createTurn,
  rememberCommand: (project, key, { signal } = {}) => request(
    `/api/v2/workbench/remember-commands/${encodeURIComponent(key)}?${threadHistoryQuery(project, 'shared')}`, { signal }),
  taskCommand: (project, key, { signal } = {}) => request(
    `/api/v2/workbench/task-commands/${encodeURIComponent(key)}?${threadHistoryQuery(project, 'shared')}`, { signal }),
  sharedTaskOutcome: (project, turnId, { signal } = {}) => request(
    `/api/v2/workbench/turns/${encodeURIComponent(turnId)}/outcome?${threadHistoryQuery(project, 'shared')}`, { signal }),
  continue: continueTurn,
  decideLexicon: async (project, turnId, proposal, { signal } = {}) => {
    const body = { project_id: project, ...proposal };
    const key = submission(body, `lexicon:${turnId}`);
    const value = await request(`/api/v2/workbench/turns/${encodeURIComponent(turnId)}/lexicon-action`, {
      method: 'POST', signal, headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key.id },
      body: JSON.stringify(body),
    });
    key.clear();
    return value;
  },
  stop: async (project, turnId, { signal } = {}) => {
    const response = await fetch(libraryBackendUrl(`/api/v2/workbench/turns/${encodeURIComponent(turnId)}/stop`), {
      method: 'POST', cache: 'no-store', signal, headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_id: project }),
    });
    const value = await readResponseJson(response);
    if (!response.ok) throw failure(value.detail);
    if (response.status !== 202 || !['stopping', 'stopped'].includes(value?.state)) throw failure('invalid_response');
    return value;
  },
  resume: async (project, turnId, options = {}) => {
    if (typeof turnId !== 'string' || !turnId) throw failure('invalid_response');
    const state = readState(project, turnId, options.after ?? 0, options.scopeKind || 'personal');
    if (options.intent === 'do') state.turnIntent = 'do';
    if (options.executor && options.executor !== 'builtin') state.externalExecutor = options.executor;
    if (options.kernelTurnId) state.externalKernel = options.kernelTurnId;
    return resumeReads(state, options);
  },
  division: (project,id,options={}) => request(`/api/v2/workbench/turns/${encodeURIComponent(id)}/division?${new URLSearchParams({project_id:project,...(options.for_redo?{for_redo:'true'}:{})})}`, { signal: options.signal }),
  saveDivision: (project,id,items,revision) => request(`/api/v2/workbench/turns/${encodeURIComponent(id)}/division`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_id:project,items,expected_revision:revision})}),
  deleteDivision: (project,id,revision) => request(`/api/v2/workbench/turns/${encodeURIComponent(id)}/division`,{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_id:project,expected_revision:revision})}),
  pendingSharedRedo, prepareSharedRedo, checkSharedRedo, submitSharedRedo, settleSharedRedo,
  redo: async (project,id,revision,options={}) => {
    const body = {project_id:project,expected_revision:revision,
      ...(options.source_kind ? {source_kind:options.source_kind} : {}),
      ...(Object.hasOwn(options, 'continue_from') ? {continue_from:options.continue_from} : {})};
    const key = submission(body, `redo:${id}`);
    try {
      const value = await request(`/api/v2/workbench/turns/${encodeURIComponent(id)}/redo`, {
        method:'POST', signal: options.signal, headers:{'Content-Type':'application/json','Idempotency-Key':key.id}, body:JSON.stringify(body),
      });
      if (!value?.turn?.id || !value?.thread_id) throw failure('invalid_response');
      key.clear(); return value;
    } catch (error) {
      // 连接或响应结果未知时保留提交身份，用户再次点击仍回读同一新轮。
      if (error.status >= 400 && error.status < 500 && error.code !== 'turn_in_progress') key.clear();
      throw error;
    }
  },
  retry: (project, id) => post(`/api/v2/workbench/turns/${encodeURIComponent(id)}/retry`, { project_id: project }),
  insight: (project, insight, action) => post(`/api/v2/library/insights/${encodeURIComponent(insight.id)}/${action}`, { project_id: project, expected_revision: insight.revision }),
  file: (project, file, files = []) => { const body = new FormData(); body.append('project_id', project); body.append('file', file); files.forEach(image => body.append('files', image)); return request('/api/v2/workbench/files', { method: 'POST', body }); },
  images: (project, itemId, { signal } = {}) => request(`/api/v2/workbench/items/${encodeURIComponent(itemId)}/images?project_id=${encodeURIComponent(project)}`, { signal }),
};
