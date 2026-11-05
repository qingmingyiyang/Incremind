export function reviewShortcut({ key, composing, repeat, modified, formControl, interactiveEnter, queueFocus, modalOpen, disabled }) {
  if (disabled || composing || repeat || modified || formControl || !queueFocus || modalOpen) return null;
  if (key === 'Enter') return interactiveEnter ? null : 'confirm';
  if (key.toLowerCase() === 'd') return 'drop';
  if (key.toLowerCase() === 'e') return 'edit';
  return null;
}

export function insightUsageCount(item) {
  return Number.isSafeInteger(item.usage_count) && item.usage_count >= 0 ? item.usage_count : null;
}
