import { readSharedCommands, saveSharedCommand } from './sharedProjectCommands.js';
import { backupId } from './sharedBackupViews.js';

const part = encodeURIComponent;
export const sharedBackupCommandPath = (kind, source) => kind === 'backup'
  ? `/${part(source)}/backups` : `/backups/${part(source)}/projects`;
function validate(command, key) {
  const kind = command?.expected?.kind, source = command?.expected?.source;
  if (!['backup', 'restore'].includes(kind) || command.storageKey !== key || !backupId(command.id)
      || typeof source !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/.test(source)
      || kind === 'backup' && !source.startsWith('shared-') || kind === 'restore' && !backupId(source)
      || command.path !== sharedBackupCommandPath(kind, source) || typeof command.label !== 'string'
      || kind === 'backup' && Object.keys(command.body).length !== 0
      || kind === 'restore' && (Object.keys(command.body).sort().join(',') !== 'approved,batch_size,name'
        || command.body.approved !== true || command.body.batch_size !== 8
        || typeof command.body.name !== 'string' || !command.body.name.trim() || command.body.name.length > 80))
    throw new Error('备份操作记录无法核对，请保留浏览器资料');
  return command;
}
export function sharedBackupCommands(key) {
  return readSharedCommands(key).map(command => validate(command, key)).reverse();
}
export function newSharedBackupCommand(kind, source, label, key, name, id = crypto.randomUUID()) {
  const command = validate({ version: 1, storageKey: key, id, label,
    path: sharedBackupCommandPath(kind, source), expected: { kind, source },
    body: kind === 'backup' ? {} : { name: name.trim(), approved: true, batch_size: 8 },
    createdAt: new Date().toISOString(), phase: 'prepared' }, key);
  saveSharedCommand(command);
  return command;
}
export function markSharedBackupCommand(command, phase) {
  validate(command, command.storageKey);
  return saveSharedCommand({ ...command, phase });
}
