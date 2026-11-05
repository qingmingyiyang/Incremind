import { readStoredJson } from '../../shared/lib/browserStorage.js';
import {
  readSharedCommands, saveSharedCommand, clearSharedCommand,
} from '../shared-projects/sharedProjectCommands.js';

const validId = value => typeof value === 'string' && /^[a-z0-9][a-z0-9_-]{7,63}$/.test(value);
export const backupScheduleFields = ['frequency', 'time', 'timezone', 'weekday', 'month_day'];
export const validBackupSchedule = value => value && ['daily', 'weekly', 'monthly', 'off'].includes(value.frequency)
  && /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(value.time) && ['UTC', 'Asia/Shanghai'].includes(value.timezone)
  && Number.isInteger(value.weekday) && value.weekday >= 0 && value.weekday < 7
  && Number.isInteger(value.month_day) && value.month_day > 0 && value.month_day <= 31;
const validScheduleCommand = value => validBackupSchedule(value) && validId(value.command_id)
  && Number.isSafeInteger(value.expected_revision) && value.expected_revision >= 0;
const pathFor = (kind, id) => kind === 'import' ? `/api/v2/backups/imports/${id}` : '/api/v2/backups/schedule';

function validate(command, kind) {
  if (!['import', 'schedule'].includes(kind) || command?.expected?.kind !== kind || !validId(command.id)
      || command.path !== pathFor(kind, command.id)
      || kind === 'schedule' && (!validScheduleCommand(command.body) || command.body.command_id !== command.id)
      || kind === 'import' && (!Object.hasOwn(command.body, 'file') || command.body.file !== null
        && (typeof command.body.file.name !== 'string' || !Number.isSafeInteger(command.body.file.size) || command.body.file.size < 0)))
    throw new Error('备份操作记录无法读取，请保留当前浏览器资料');
  return command;
}

export function createBackupCommand(kind, storageKey, id, body) {
  return validate({ version: 1, storageKey, id, path: pathFor(kind, id), body,
    expected: { kind }, phase: 'prepared', createdAt: new Date().toISOString() }, kind);
}

export function readBackupCommands(key, kind, legacyKey) {
  // 原入口的单编号记录只迁入独立记录，保留旧键以兼容仍打开的旧页面。
  if (legacyKey) {
    const legacy = readStoredJson(legacyKey);
    if (legacy.unavailable || legacy.invalid) throw new Error('备份操作记录暂不可读');
    if (legacy.value !== null) {
      const id = legacy.value?.command_id;
      if (!validId(id)) throw new Error('备份操作编号无法读取');
      const existing = readStoredJson(`${key}:operation:${encodeURIComponent(id)}:record`);
      if (existing.unavailable || existing.invalid) throw new Error('备份操作记录暂不可读');
      if (!existing.value) saveBackupCommand(createBackupCommand(kind, key, id,
        kind === 'import' ? { file: null } : legacy.value));
    }
  }
  return readSharedCommands(key).map(command => validate(command, kind));
}

export function saveBackupCommand(command, phase = command.phase) {
  validate(command, command.expected?.kind);
  return saveSharedCommand({ ...command, phase });
}

export function settleBackupCommand(command) {
  validate(command, command.expected?.kind);
  clearSharedCommand(command);
}

export function isBackupCommandSettled(command) {
  const result = readStoredJson(`${command.storageKey}:operation:${encodeURIComponent(command.id)}:settled`);
  if (result.unavailable || result.invalid || result.value && result.value.id !== command.id)
    throw new Error('备份操作完成记录暂不可读');
  return result.value?.id === command.id;
}

export function canResendBackupFile(command, selected, held) {
  return Boolean(command && selected && held?.id === command.id && held.file === selected);
}
