export function formatRange(startSeconds, endSeconds) {
  return `${formatTimestamp(startSeconds)} - ${formatTimestamp(endSeconds)}`;
}

export function formatTimestamp(totalSeconds) {
  const safeSeconds = Math.max(0, Math.floor(totalSeconds ?? 0));
  const hours = Math.floor(safeSeconds / 3600);
  const minutes = Math.floor((safeSeconds % 3600) / 60);
  const seconds = safeSeconds % 60;

  if (hours > 0) {
    return `${pad(hours)}:${pad(minutes)}:${pad(seconds)}`;
  }
  return `${pad(minutes)}:${pad(seconds)}`;
}

function pad(value) {
  return String(value).padStart(2, "0");
}

export function formatLocalShortDate(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return '—';
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function formatLocalDate(value) {
  if (!value) return '—';
  // 只有日期的事实保持原年月日，时间点使用浏览器本地日期。
  if (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)) return value.replaceAll('-', '·');
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return '—';
  return `${date.getFullYear()}·${pad(date.getMonth() + 1)}·${pad(date.getDate())}`;
}
