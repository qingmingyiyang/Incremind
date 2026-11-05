import { browserStorage, readStoredJson, removeStoredItem, writeStoredJson } from '../../shared/lib/browserStorage.js';
import { sharedProjectDetail } from './sharedProjectViews.js';

const part = value => encodeURIComponent(value);
const phases = new Set(['prepared', 'sending', 'unknown', 'not_received', 'pending']);
const recordKey = command => `${command.storageKey}:operation:${encodeURIComponent(command.id)}:record`;
const stateKey = command => `${command.storageKey}:operation:${encodeURIComponent(command.id)}:state`;
const settledKey = command => `${command.storageKey}:operation:${encodeURIComponent(command.id)}:settled`;
function read(key, storage) {
  const result = readStoredJson(key, storage);
  if (result.unavailable) throw new Error('浏览器无法保存操作记录，请恢复本地存储后重试');
  if (result.invalid) throw new Error('待核对操作记录无法读取，请保留当前浏览器资料');
  return result.value;
}
function sameRequest(left, right) {
  return left.id === right.id && left.storageKey === right.storageKey && left.path === right.path
    && JSON.stringify(left.body) === JSON.stringify(right.body)
    && JSON.stringify(left.expected) === JSON.stringify(right.expected);
}
export function readSharedCommands(key) {
  const storage = browserStorage();
  if (!storage) throw new Error('浏览器无法保存操作记录，请恢复本地存储后重试');
  const commands = [];
  for (let index = 0; index < storage.length; index += 1) {
    const slot = storage.key(index);
    if (!slot?.startsWith(`${key}:operation:`) || !slot.endsWith(':record')) continue;
    const value = read(slot, storage);
    if (!value || value.version !== 1 || value.storageKey !== key || typeof value.id !== 'string'
        || typeof value.path !== 'string' || !value.body || !value.expected || recordKey(value) !== slot)
      throw new Error('待核对操作记录无法读取，请保留当前浏览器资料');
    const settled = read(settledKey(value), storage);
    if (settled) {
      if (settled.id !== value.id) throw new Error('操作完成记录不匹配，请保留当前浏览器资料');
      continue;
    }
    const state = read(stateKey(value), storage);
    if (state && (state.id !== value.id || !phases.has(state.phase)))
      throw new Error('操作状态记录不匹配，请保留当前浏览器资料');
    commands.push({ ...value, phase: state?.phase || 'prepared' });
  }
  return commands.sort((left, right) => left.createdAt.localeCompare(right.createdAt) || left.id.localeCompare(right.id));
}
export function readSharedCommand(key) {
  return readSharedCommands(key)[0] || null;
}
export function saveSharedCommand(command) {
  if (!phases.has(command.phase)) throw new Error('操作状态无效');
  const storage = browserStorage();
  const existing = read(recordKey(command), storage);
  if (existing && !sameRequest(existing, command)) throw new Error('操作标识对应的内容不同，请保留当前操作记录');
  if (read(settledKey(command), storage)) throw new Error('该操作已处理，请重新核对待处理操作');
  // 命令正文创建后保持不变；每个 ID 的状态与完成标记互相独立。
  if ((!existing && !writeStoredJson(recordKey(command), { ...command, phase: 'prepared' }, storage))
      || !writeStoredJson(stateKey(command), { id: command.id, phase: command.phase }, storage))
    throw new Error('操作记录未保存，请恢复本地存储后重试');
  return command;
}
export function clearSharedCommand(command, { purgeRecord = false } = {}) {
  const storage = browserStorage(), existing = read(recordKey(command), storage);
  if (!existing || !sameRequest(existing, command)) throw new Error('操作记录已变化，请重新核对');
  // 不删除其他标签页的记录；独立完成标记也不会被迟到的状态保存覆盖。
  if (!writeStoredJson(settledKey(command), { id: command.id, settledAt: new Date().toISOString() }, storage))
    throw new Error('操作完成记录未保存，请恢复本地存储后重试');
  if (purgeRecord) {
    if (!removeStoredItem(recordKey(command), storage)) throw new Error('本机原请求正文未清理');
    removeStoredItem(stateKey(command), storage);
  }
}
export function createSharedCommand(spec, storageKey, options) {
  const id = spec.kind === 'resume' ? spec.operation.operation_id : crypto.randomUUID();
  const project = spec.project, member = spec.member;
  let path, body, inputs, action = spec.kind;
  if (action === 'create') {
    path = ''; body = { project_name: spec.projectName, operation_id: id }; inputs = { name: spec.projectName.trim() };
  } else if (action === 'accept') {
    path = `/invitations/${part(spec.invitation.id)}/accept`;
    body = { expected_revision: spec.invitation.revision, operation_id: id };
    inputs = { invitation_id: spec.invitation.id, revision: spec.invitation.revision };
  } else if (action === 'invite') {
    path = `/${part(project.id)}/invitations`;
    body = { user_id: spec.userId, role: spec.role, authority: project.authority, operation_id: id };
    inputs = { user_id: spec.userId, role: spec.role };
  } else if (action === 'delete') {
    path = `/${part(project.id)}/delete`;
    body = { confirmation: spec.confirmation, authority: project.authority, operation_id: id, skip_backup: true };
    inputs = { confirmation: spec.confirmation, skip_backup: true };
  } else if (action === 'resume') {
    path = `/operations/${part(id)}/resume`; body = {}; inputs = spec.operation.inputs;
    action = spec.operation.action;
  } else if (['role', 'remove', 'leave', 'transfer'].includes(action)) {
    path = `/${part(project.id)}/members/${part(member.user_id)}`;
    body = { action, role: spec.role ?? null, expected_revision: member.revision, authority: project.authority, operation_id: id };
    inputs = { user_id: member.user_id, role: spec.role ?? null, revision: member.revision };
  } else throw new Error('共享操作类型无效');
  return JSON.parse(JSON.stringify({ version: 1, storageKey, id, path, body, phase: 'prepared',
    label: spec.projectName?.trim() || project?.name || spec.invitation?.project_name || spec.operation?.project_id,
    projectId: project?.id || spec.operation?.project_id || spec.invitation?.project_id || null,
    expected: { action, inputs, ...(spec.actorUserId || spec.operation?.actor_user_id
      ? { actor_user_id: spec.operation?.actor_user_id || spec.actorUserId } : {}) }, options, createdAt: new Date().toISOString() }));
}
export function validateSharedOperation(command, operation, actorUserId) {
  const expectedActor = command.expected.actor_user_id || (!command.path.endsWith('/resume') ? actorUserId : undefined);
  if (!operation || operation.operation_id !== command.id || operation.action !== command.expected.action
      || !['pending', 'completed', 'cancelled'].includes(operation.state)
      || !Number.isSafeInteger(operation.revision) || operation.revision < 1
      || typeof operation.actor_user_id !== 'string' || !operation.actor_user_id
      || expectedActor !== undefined && operation.actor_user_id !== expectedActor
      || command.projectId && operation.project_id !== command.projectId
      || operation.state !== 'cancelled' && (typeof operation.project_id !== 'string' || !operation.project_id)
      || Object.entries(command.expected.inputs).some(([key, value]) => JSON.stringify(operation.inputs?.[key]) !== JSON.stringify(value)))
    throw new Error('操作标识对应的内容不同，请保留当前操作记录');
  return operation;
}
export function validateSharedAcknowledgement(command, value) {
  if (command.expected.action === 'create' && value?.project) {
    sharedProjectDetail(value, value.project.id);
    if (value.project.name !== command.expected.inputs.name
        || command.expected.actor_user_id && value.project.actor_user_id !== command.expected.actor_user_id)
      throw new Error('提交结果尚未核对，请读取原操作');
    return value;
  }
  if (!value || value.operation_id !== command.id || !['pending', 'completed'].includes(value.state)
      || typeof value.project_id !== 'string' || !value.project_id
      || command.projectId && value.project_id !== command.projectId)
    throw new Error('提交结果尚未核对，请读取原操作');
  return value;
}
export function sharedCommandBlocks(command, spec) {
  if (spec.kind === 'create') return command.expected.action === 'create'
    && command.expected.inputs.name === spec.projectName.trim();
  return Boolean(command.projectId && command.projectId === (spec.project?.id || spec.invitation?.project_id));
}
export function validateSharedCommand(command, storageKey) {
  const segment = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/.test(value);
  const invalid = () => { throw new Error('共享操作记录无法核对，请保留本机记录'); };
  if (!command || command.storageKey !== storageKey || !segment(command.id) || !phases.has(command.phase)
      || typeof command.path !== 'string' || !command.body || Array.isArray(command.body)
      || !command.expected || !command.expected.inputs || Array.isArray(command.expected.inputs)) invalid();
  const { action, inputs } = command.expected, body = command.body;
  if (!['create','invite','accept','role','remove','leave','transfer','delete'].includes(action)
      || command.expected.actor_user_id !== undefined && !segment(command.expected.actor_user_id)) invalid();
  if (command.path === `/operations/${part(command.id)}/resume`) {
    if (!segment(command.projectId) || Object.keys(body).length) invalid();
    return command;
  }
  let expectedPath, keys;
  if (action === 'create') {
    expectedPath = ''; keys = ['project_name','operation_id'];
    if (typeof body.project_name !== 'string' || !body.project_name.trim()
        || body.project_name.trim() !== inputs.name) invalid();
  } else if (action === 'accept') {
    expectedPath = `/invitations/${part(inputs.invitation_id)}/accept`; keys = ['expected_revision','operation_id'];
    if (!segment(inputs.invitation_id) || body.expected_revision !== inputs.revision) invalid();
  } else {
    if (!segment(command.projectId)) invalid();
    if (action === 'invite') {
      expectedPath = `/${part(command.projectId)}/invitations`; keys = ['user_id','role','authority','operation_id'];
      if (body.user_id !== inputs.user_id || body.role !== inputs.role || !segment(inputs.user_id)
          || !['member','viewer'].includes(inputs.role)) invalid();
    } else if (action === 'delete') {
      expectedPath = `/${part(command.projectId)}/delete`; keys = ['confirmation','authority','operation_id','skip_backup'];
      if (body.confirmation !== inputs.confirmation || body.skip_backup !== true || inputs.skip_backup !== true) invalid();
    } else {
      expectedPath = `/${part(command.projectId)}/members/${part(inputs.user_id)}`;
      keys = ['action','role','expected_revision','authority','operation_id'];
      if (!segment(inputs.user_id) || body.action !== action || body.role !== inputs.role
          || body.expected_revision !== inputs.revision || (action === 'role'
            ? !['member','viewer'].includes(body.role) : body.role !== null)) invalid();
    }
    const authority = body.authority;
    if (!authority || Object.keys(authority).sort().join(',') !== 'account_revision,member_revision,project_revision'
        || !Number.isSafeInteger(authority.project_revision) || authority.project_revision < 1
        || !Number.isSafeInteger(authority.member_revision) || authority.member_revision < 0
        || !Number.isSafeInteger(authority.account_revision) || authority.account_revision < 1) invalid();
  }
  if (command.path !== expectedPath || body.operation_id !== command.id
      || Object.keys(body).sort().join(',') !== keys.sort().join(',')
      || 'expected_revision' in body && (!Number.isSafeInteger(body.expected_revision) || body.expected_revision < 1)) invalid();
  return command;
}
