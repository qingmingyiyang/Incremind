export const backupId = value => typeof value === 'string' && /^[a-z0-9][a-z0-9_-]{7,63}$/.test(value);
const objectId = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/.test(value);
const count = value => Number.isSafeInteger(value) && value >= 0;
const invalid = () => { throw new Error('备份或恢复记录未核对，请重新读取'); };

export function sharedBackup(value, expected = {}) {
  if (!backupId(value?.command_id) || !objectId(value.project_id) || !value.project_id.startsWith('shared-')
      || value.scope !== 'shared-project' || !['preparing', 'archived', 'ready', 'failed'].includes(value.state)
      || !Number.isSafeInteger(value.revision) || value.revision < 1 || typeof value.project_name !== 'string'
      || !value.project_name.trim() || typeof value.created_at !== 'string' || !Number.isFinite(Date.parse(value.created_at))
      || value.snapshot_id !== `shared-${value.command_id}`
      || value.size_bytes !== undefined && (!count(value.size_bytes) || value.size_bytes === 0)
      || ['archived', 'ready'].includes(value.state) && !count(value.size_bytes)
      || expected.id && value.command_id !== expected.id || expected.project && value.project_id !== expected.project) invalid();
  return value;
}

export function ownedSharedBackups(value) {
  if (!Array.isArray(value?.items)) invalid();
  const rows = value.items.map(row => sharedBackup(row));
  if (new Set(rows.map(row => row.command_id)).size !== rows.length) invalid();
  return rows;
}

export function sharedBackupImport(value, expected = {}) {
  if (!backupId(value?.command_id) || !backupId(value.backup_id) || !objectId(value.project_id)
      || !value.project_id.startsWith('project-') || typeof value.name !== 'string' || !value.name.trim()
      || !['preparing', 'copying', 'completed'].includes(value.state) || !count(value.completed) || !count(value.total)
      || value.completed > value.total || value.state === 'completed' && value.completed !== value.total
      || value.requires_resume !== (value.state !== 'completed') || typeof value.requires_privacy_review !== 'boolean'
      || !Number.isSafeInteger(value.revision) || value.revision < 1
      || expected.id && value.command_id !== expected.id || expected.backup && value.backup_id !== expected.backup
      || expected.project && value.project_id !== expected.project) invalid();
  return value;
}
