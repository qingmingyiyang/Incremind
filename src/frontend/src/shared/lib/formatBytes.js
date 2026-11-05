export function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return '—';
  const units = ['KB', 'MB', 'GB'];
  let value = bytes / 1024, unit = 0;
  if (value > 0 && value < 0.01) return '< 0.01 KB';
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${Number(value.toFixed(2))} ${units[unit]}`;
}
