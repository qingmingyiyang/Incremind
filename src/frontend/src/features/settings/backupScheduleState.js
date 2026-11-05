import { backupScheduleFields } from './backupCommands.js';

const select = value => Object.fromEntries(backupScheduleFields.map(key => [key, value[key]]));
const same = (left, right) => left && right && backupScheduleFields.every(key => left[key] === right[key]);

export function reconcileBackupSchedule(state, value, { preserveDraft = true, acknowledge = false } = {}) {
  const edited = state.draft && (!state.actual || !same(state.actual, state.draft));
  const preserve = state.draft && (state.pending || preserveDraft && (edited || state.conflict));
  const draft = preserve ? state.draft : select(value);
  const changed = state.actual && value.revision !== state.actual.revision;
  const conflict = Boolean(!same(draft, value)
    && (changed || state.conflict && !acknowledge));
  return { actual: value, draft, conflict,
    error: conflict ? state.conflict && changed ? '设置再次变化 · 草稿保留' : '设置已变化 · 草稿保留' : '' };
}
