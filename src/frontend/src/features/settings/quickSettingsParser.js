const weekdays = ['一', '二', '三', '四', '五', '六', '日'];

export function buildQuickPreview(change, session) {
  if (change?.error) throw new Error(change.error);
  return change ? { ...change, session } : null;
}

export function parseQuickAddition(text, projects) {
  const phrase = text.trim();
  const constraint = /^(?:给|为)?#?(.+?)(?:添加|新增)约束\s*[:：]([\s\S]*)$/.exec(phrase);
  if (constraint) {
    const name = constraint[1].trim(), content = constraint[2].trim();
    if (!content || Array.from(content).length > 100000) return { error: '填写约束正文 · 最多十万字' };
    if (projects === undefined) return { kind: 'constraint', projectName: name, content };
    const matches = projects.filter(project => project.name === name);
    if (matches.length !== 1) return { error: '写明一个完整项目名称 · 重新输入' };
    return { kind: 'constraint', project: { id: matches[0].id, name: matches[0].name }, content };
  }
  if (/约束/.test(phrase)) return { error: '写明项目名称添加约束：正文' };
  const schedule = /^(?:(?:改为|改成|设置为|设为|开启|启用)\s*)?每周\s*([一二三四五六日天])?\s*((?:[01]?\d|2[0-3]):[0-5]\d)?\s*(?:自动)?备份$/.exec(phrase);
  if (schedule) {
    const day = schedule[1] === '天' ? '日' : schedule[1];
    return { kind: 'schedule', weekday: day ? weekdays.indexOf(day) : null,
      time: schedule[2] ? schedule[2].padStart(5, '0') : null };
  }
  if (/每周|备份/.test(phrase)) return { error: '写明每周几和时间备份 · 例如每周二 09:00 备份' };
  return null;
}

export function weeklySchedule(change, actual) {
  return { frequency: 'weekly', weekday: change.weekday ?? actual.weekday,
    time: change.time ?? actual.time, timezone: actual.timezone, month_day: actual.month_day };
}

export function scheduleLabel(value) {
  const frequency = { daily: '每天', weekly: `每周${weekdays[value.weekday]}`, monthly: `每月${value.month_day}日`, off: '关闭' }[value.frequency];
  return value.frequency === 'off' ? frequency : `${frequency} ${value.time} · ${value.timezone}`;
}

export function sameConstraint(row, change) {
  return row?.id === change.id && row.project_id === change.project.id && row.revision === 1
    && row.content === change.content && row.enabled === true && row.valid_from === null && row.valid_until === null;
}

const exact = (value, fields) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).length === fields.length && fields.every(field => Object.hasOwn(value, field));

export function constraintFromCommand(command, storageKey) {
  const body = command?.body, expected = command?.expected;
  if (!exact(command, ['version', 'storageKey', 'id', 'path', 'body', 'expected', 'phase', 'createdAt'])
      || command.version !== 1 || command.storageKey !== storageKey
      || typeof command.id !== 'string' || !/^constraint-[a-z0-9-]{1,100}$/i.test(command.id)
      || command.path !== `/api/recognition/constraints/${encodeURIComponent(command.id)}`
      || typeof command.phase !== 'string' || typeof command.createdAt !== 'string' || !Number.isFinite(Date.parse(command.createdAt))
      || !exact(expected, ['kind', 'project']) || expected.kind !== 'constraint'
      || !exact(expected.project, ['id', 'name']) || typeof expected.project.id !== 'string' || !expected.project.id
      || typeof expected.project.name !== 'string' || !expected.project.name
      || !exact(body, ['project_id', 'expected_revision', 'content', 'enabled', 'valid_from', 'valid_until'])
      || body.project_id !== expected.project.id || body.expected_revision !== 0 || body.enabled !== true
      || body.valid_from !== null || body.valid_until !== null || typeof body.content !== 'string'
      || !body.content.trim() || Array.from(body.content).length > 100000) throw new Error('约束操作记录无法读取，请保留浏览器资料');
  return { kind: 'constraint', id: command.id, project: { ...expected.project }, content: body.content, command, session: null };
}

export function createConstraintCommand(change, storageKey, createdAt) {
  const command = { version: 1, storageKey, id: change.id,
    path: `/api/recognition/constraints/${encodeURIComponent(change.id)}`,
    body: { project_id: change.project.id, expected_revision: 0, content: change.content,
      enabled: true, valid_from: null, valid_until: null },
    expected: { kind: 'constraint', project: { ...change.project } }, phase: 'prepared', createdAt };
  constraintFromCommand(command, storageKey);
  return command;
}
