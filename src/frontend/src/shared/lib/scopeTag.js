const bare = /^[^\s/#]+$/u;
const legacy = '#([^\\s/#]+)(?:/([^\\s/#]+))?';

export function formatScopeTag(project, scene = null) {
  if (typeof project !== 'string' || !project.trim() || scene !== null && (typeof scene !== 'string' || !scene.trim())) throw new Error('invalid_scope_name');
  return bare.test(project) && (scene === null || bare.test(scene))
    ? `#${project}${scene === null ? '' : '/' + scene}` : '# ' + JSON.stringify([project, scene]);
}

export function scopedText(text) {
  const matches = [new RegExp('^[ \\t]*' + legacy + '(?=[ \\t]|\\r?$)[ \\t]*', 'm').exec(text),
    new RegExp('[ \\t]+' + legacy + '[ \\t]*(?=\\r?$)', 'm').exec(text)]
    .filter(Boolean).map(match => ({ index: match.index, end: match.index + match[0].length, project: match[1], scene: match[2] || null, encoded: false }));
  // 扩展标记只接受完整单行 JSON，正文与旧标签原样保留。
  for (const match of text.matchAll(/^[ \t]*#[ \t]+(\[[^\r\n]*\])[ \t]*\r?$/gm)) {
    let value;
    try { value = JSON.parse(match[1]); } catch { continue; }
    if (!Array.isArray(value) || value.length !== 2 || typeof value[0] !== 'string' || !value[0].trim()
      || value[1] !== null && (typeof value[1] !== 'string' || !value[1].trim())) continue;
    matches.push({ index: match.index, end: match.index + match[0].length, project: value[0], scene: value[1], encoded: true });
  }
  const match = matches.sort((left, right) => left.index - right.index)[0];
  if (!match) return { text: text.trim(), tag: null, project: null, scene: null, encoded: false };
  return { ...match, text: (text.slice(0, match.index) + text.slice(match.end)).trim(),
    tag: text.slice(match.index, match.end).trim(), label: `#${match.project}${match.scene === null ? '' : '/' + match.scene}` };
}

export function displayScopedText(text) {
  if (typeof text !== 'string') return text;
  const parsed = scopedText(text);
  return parsed.encoded ? parsed.text : text;
}
