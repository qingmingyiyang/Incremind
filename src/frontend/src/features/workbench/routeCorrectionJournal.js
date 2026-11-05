import { readSharedCommands, saveSharedCommand, clearSharedCommand } from '../shared-projects/sharedProjectCommands.js';

export const ROUTE_CORRECTION_EVENT = 'chriptmas-route-correction-operations';
export function commands(scope) {
  return readSharedCommands(scope).map(command => {
    if (!['route-correction', 'route-example-delete'].includes(command.path) || Object.keys(command.body).some(key => !['turnId', 'projectId'].includes(key))) throw new Error('纠正记录无法核对');
    return command;
  });
}
function notify() { globalThis.window?.dispatchEvent(new Event(ROUTE_CORRECTION_EVENT)); }
export function prepare(scope, turnId, projectId, path = 'route-correction') {
  if (commands(scope).some(command => command.body.turnId === turnId)) throw new Error('已有纠正待核对');
  const command = { version: 1, storageKey: scope, id: crypto.randomUUID(), path,
    body: { turnId, projectId }, expected: { turnId, projectId }, phase: 'prepared', createdAt: new Date().toISOString() };
  saveSharedCommand(command); notify(); return command;
}
export function mark(command, phase) { saveSharedCommand({ ...command, phase }); notify(); }
export function settle(command) { clearSharedCommand(command); notify(); }
