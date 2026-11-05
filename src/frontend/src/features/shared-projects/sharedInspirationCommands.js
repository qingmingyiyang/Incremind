import { readSharedCommands, saveSharedCommand } from './sharedProjectCommands.js';

const segment = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/.test(value);
const invalid = () => { throw new Error('灵感记录无法核对 · 保留浏览器资料'); };
export function validateSharedInspirationCommand(command, key, project) {
  if (!command || command.storageKey !== key || !segment(command.id) || !segment(project)
      || command.projectId !== project || command.path !== `/${encodeURIComponent(project)}/intake/inspiration`
      || command.expected?.kind !== 'inspiration' || command.expected?.scope_kind !== 'shared'
      || Object.keys(command.body || {}).join(',') !== 'text' || typeof command.body.text !== 'string'
      || !command.body.text.trim() || typeof command.createdAt !== 'string'
      || !Number.isFinite(Date.parse(command.createdAt))) invalid();
  return command;
}
export function sharedInspirationCommands(key, project) {
  return readSharedCommands(key).map(command => validateSharedInspirationCommand(command, key, project));
}
export function newSharedInspirationCommand(key, project, text, id = crypto.randomUUID()) {
  const command = validateSharedInspirationCommand({ version: 1, storageKey: key, id, projectId: project,
    path: `/${encodeURIComponent(project)}/intake/inspiration`, body: { text }, phase: 'prepared',
    expected: { kind: 'inspiration', scope_kind: 'shared' }, createdAt: new Date().toISOString() }, key, project);
  saveSharedCommand(command);
  return command;
}
export function markSharedInspirationCommand(command, phase) {
  validateSharedInspirationCommand(command, command.storageKey, command.projectId);
  return saveSharedCommand({ ...command, phase });
}
export function sharedInspirationResult(value, command) {
  if (value?.command_id !== command.id || value.project_id !== command.projectId || value.scope_kind !== 'shared'
      || value.kind !== 'inspiration' || value.state !== 'accepted' || typeof value.namespace !== 'string' || !value.namespace
      || !segment(value.insight?.id) || !Number.isSafeInteger(value.insight.revision) || value.insight.revision < 1
      || typeof value.insight.content !== 'string' || !['pending', 'published', 'rejected'].includes(value.insight.state)) invalid();
  return value;
}
