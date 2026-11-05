const identity = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/.test(value);

export function insightSourceReference(source) {
  if (source?.via !== 'steer') return null;
  const hasText = typeof source.text === 'string';
  const text = hasText ? Array.from(source.text).slice(0, 12).join('') : '';
  return { text, reference: hasText && identity(source.turn) && identity(source.project_id)
    ? { project_id: source.project_id, turn_id: source.turn } : null };
}
