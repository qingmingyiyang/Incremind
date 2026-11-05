const pad = (value, length = 2) => String(value).padStart(length, '0');

export function localConstraintTime(value) {
  if (!value) return '';
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) throw new Error('有效时间无效 · 重新选择');
  return `${pad(date.getFullYear(), 4)}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${pad(date.getMilliseconds(), 3)}`;
}

export function constraintDateFields(row = {}) {
  return { valid_from: localConstraintTime(row.valid_from), valid_until: localConstraintTime(row.valid_until),
    time_zone: Intl.DateTimeFormat().resolvedOptions().timeZone };
}

export function rebaseConstraintDraft(draft, base, useServer = false) {
  return { ...draft, base, ...(useServer ? { content: base.content, ...constraintDateFields(base) } : {}) };
}

function storedTime(value, original) {
  if (value === localConstraintTime(original)) return original || null;
  if (!value) return null;
  const date = new Date(value);
  // 浏览器会把夏令时跳过的本地时间自动推进，保存前拒绝这类改写。
  if (!Number.isFinite(date.getTime()) || localConstraintTime(date).slice(0, value.length) !== value)
    throw new Error('有效时间无效 · 重新选择');
  return date.toISOString();
}

export function constraintDatePayload(draft) {
  if ((draft.valid_from || draft.valid_until) && draft.time_zone
      && draft.time_zone !== Intl.DateTimeFormat().resolvedOptions().timeZone)
    throw new Error('本地时区变化 · 重新打开');
  const validFrom = storedTime(draft.valid_from, draft.base?.valid_from);
  const validUntil = storedTime(draft.valid_until, draft.base?.valid_until);
  if (validFrom && validUntil && new Date(validFrom) >= new Date(validUntil))
    throw new Error('结束须晚于开始 · 修改时间');
  return { validFrom, validUntil };
}

export function constraintDateError(draft) {
  try { constraintDatePayload(draft); return ''; }
  catch (failure) { return failure.message; }
}

export function constraintDateComparison(row, localDraft = false) {
  const fields = localDraft ? constraintDatePayload(row) : { validFrom: row.valid_from, validUntil: row.valid_until };
  return { content: row.content, valid_from: fields.validFrom || '不限', valid_until: fields.validUntil || '不限' };
}

export function constraintTimeSummary(row, now = new Date()) {
  if (!row.enabled) return '已停用';
  if (row.valid_until && new Date(row.valid_until) <= now) return '已到期';
  if (row.valid_from && new Date(row.valid_from) > now) return '尚未开始';
  return '生效';
}
