export const interventionTextLength = text => Array.from(text).length;
export function mergeInterventions(current = [], incoming = []) {
  const entries = new Map(current.map(entry => [entry.id, entry]));
  for (const entry of incoming) {
    const previous = entries.get(entry.id);
    if (!previous || previous.state === 'queued' || entry.state !== 'queued') entries.set(entry.id, entry);
  }
  return [...entries.values()];
}
export function updatePartAnswer(answers = {}, part, text, replace = false) {
  if (!Number.isSafeInteger(part) || part < 0 || typeof text !== 'string') throw new Error('回答部分无效');
  return { ...answers, [part]: (replace ? '' : answers[part] || '') + text };
}
export function acceptedIntervention(value, submitted) {
  const entry = value?.steer;
  if (!entry || typeof entry.id !== 'string' || !entry.id || value.steer_id !== entry.id
    || value.state !== entry.state || !['queued', 'applied', 'unapplied'].includes(entry.state)
    || entry.text !== submitted.text || typeof entry.at !== 'string' || !entry.applied_to
    || !(entry.applied_to.part === null || Number.isSafeInteger(entry.applied_to.part) && entry.applied_to.part >= 0)
    || !(entry.applied_to.step === null || typeof entry.applied_to.step === 'string'))
    throw new Error('插话提交身份不一致');
  return entry;
}
