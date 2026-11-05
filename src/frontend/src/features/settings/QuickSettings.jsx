import { useEffect, useRef, useState } from 'react';
import { Icon } from '../../shared/ui';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { deviceIdentity, isDeviceIdentity, userStorageKey } from '../../shared/api/deviceTransport';
import { recognitionApi } from '../../shared/api/recognitionApi';
import { applyTheme, readStoredTheme } from '../rebuild/themeBootstrap';
import { loadBackupSchedule, loadBackupScheduleCommand, newBackupCommand, saveBackupSchedule } from '../rebuild/localMemoryVaultApi';
import { backupScheduleFields, createBackupCommand, readBackupCommands, saveBackupCommand, settleBackupCommand, validBackupSchedule } from './backupCommands';
import { buildQuickPreview, parseQuickAddition, scheduleLabel, sameConstraint, weeklySchedule, createConstraintCommand, constraintFromCommand } from './quickSettingsParser';
import { readSharedCommands, saveSharedCommand, clearSharedCommand } from '../shared-projects/sharedProjectCommands';

const labels = { generation: '生成', vision: '识图', asr: '转写', embedding: '向量', rerank: '重排', search: '搜索' };
const conflict = () => Object.assign(new Error('设置已变化 · 重新预览'), { status: 409 });

function changeOf(text, value, projects) {
  const phrase = text.trim();
  if (!phrase) return null;
  const addition = parseQuickAddition(phrase, projects);
  if (addition) return addition;
  if (/不要|别|不必|不用|不需要|不想|不打算|不取消|不关闭|不停止|不开启|不打开|不启用|不允许|禁止关闭|禁止取消|禁止停止/.test(phrase)
      || /并|同时|以及|然后|再把|还有|[,，;；、]/.test(phrase)) return { error: '一次写一项直接操作 · 重新输入' };
  if ([/跟随系统/, /深色|暗色/, /浅色|亮色/].filter(pattern => pattern.test(phrase)).length > 1) return { error: '一次选择一种主题 · 重新输入' };
  const theme = /跟随系统/.test(phrase) ? 'system' : /深色|暗色/.test(phrase) ? 'dark' : /浅色|亮色/.test(phrase) ? 'light' : null;
  if (theme) {
    if (/关闭|取消|关掉|停止|禁止|停用|不/.test(phrase) || /外发|私密/.test(phrase)) return { error: '直接写切换到哪种主题 · 重新输入' };
    return { kind: 'theme', label: '主题', from: readStoredTheme(), to: theme, display: { light: '浅色', dark: '深色', system: '跟随系统' }[theme] };
  }
  if (/私密/.test(phrase)) {
    if (/外发|开启|打开|允许|禁止/.test(phrase)) return { error: '直接写项目设为私密或取消私密 · 重新输入' };
    const matches = projects.filter(project => phrase.includes(project.name));
    if (matches.length !== 1) return { error: '写明一个项目名称 · 重新输入' };
    const project = matches[0], enabled = !/取消|关闭|移除|不设|解除|不再|不私密|非私密/.test(phrase);
    return { kind: 'privacy', label: `${project.name} · 私密`, from: value.privacy.private_projects.includes(project.id), to: enabled, project, revision: value.privacy.revision };
  }
  const local = /本机/.test(phrase), remote = /外接|云端/.test(phrase);
  if (local && remote) return { error: '一次选择一种处理方式 · 重新输入' };
  const purposeMatches = Object.entries(labels).filter(([, label]) => phrase.includes(label));
  if (purposeMatches.length === 1 && (local || remote) && !/外发/.test(phrase)) {
    if (/关闭|取消|停止|禁止|停用|关掉/.test(phrase)
        || !/切换|切到|换成|换到|换为|改成|改为|改用|设为|使用|用本机|用云端|用外接/.test(phrase)) return { error: '直接写切换到本机或外接 · 重新输入' };
    const [purpose, label] = purposeMatches[0], row = value.model[purpose];
    if (!['vision', 'embedding'].includes(purpose)) return { error: '此方式在用途配置中选择 · 打开模型' };
    return { kind: 'mode', purpose, label: `${label} · 方式`, from: row.mode || 'remote', to: local ? 'local' : 'remote', display: local ? '本机' : '外接', revision: row.mode_revision ?? 0 };
  }
  const off = /关闭|关掉|停止|禁止|不外发|不允许|不要|停用/.test(phrase), on = /开启|打开|允许|启用/.test(phrase);
  if (off === on) return { error: '写明开启或关闭及用途 · 重新输入' };
  const enabled = on;
  if (/外部\s*agent/i.test(phrase) && /外发/.test(phrase)) return { kind: 'agent', label: '外部 agent · 外发', from: value.external_agent.allow_remote, to: enabled, revision: value.external_agent.revision };
  if (purposeMatches.length !== 1 || !/外发/.test(phrase)) return { error: '写明一个用途及外发操作 · 重新输入' };
  const [purpose, label] = purposeMatches[0], row = value.model[purpose];
  if (purpose === 'asr') return { error: '转写授权在模型中设置 · 打开模型' };
  if (purpose === 'generation' && value.model.generation_mode.mode !== 'api' || ['vision', 'embedding'].includes(purpose) && row.mode === 'local') return { error: '当前使用本机或订阅方式 · 打开模型' };
  return { kind: 'egress', purpose, label: `${label} · 外发`, from: row.allow_remote === true, to: enabled, revision: row.revision };
}

const stateLabel = value => typeof value === 'boolean' ? value ? '开' : '关' : { local: '本机', remote: '外接', api: 'API', system: '跟随系统', light: '浅色', dark: '深色' }[value] || value;

export function QuickSettings({ value, projects, api, run, busy, onBackupScheduleChange }) {
  const [text, setText] = useState(''), [preview, setPreview] = useState(null), [saved, setSaved] = useState('');
  const [epoch, setEpoch] = useState(0), [working, setWorking] = useState(false), [error, setError] = useState('');
  const [pending, setPending] = useState(null);
  const edit = useRef(0), operation = useRef(null), pendingRef = useRef(null);
  const actor = deviceIdentity(), ownerKey = userStorageKey('quick-settings');
  const origin = globalThis.electronAPI?.backendBaseUrl || globalThis.location?.origin || '';
  const commandKey = userStorageKey(`backup-schedule-operations:${origin}`), legacyKey = userStorageKey(`backup-schedule-command:${origin}`);
  const constraintKey = userStorageKey(`quick-constraint-operations:${origin}`);
  const gate = useRequestScope(`${ownerKey}:${epoch}`), scope = gate.scope;
  const [presentationScope, setPresentationScope] = useState(scope), visible = presentationScope === scope;
  const current = token => gate.isCurrent(token) && isDeviceIdentity(actor) && userStorageKey('quick-settings') === ownerKey;
  const updatePending = next => { pendingRef.current = next; setPending(next); };
  const readConstraintPending = () => readSharedCommands(constraintKey).map(command => constraintFromCommand(command, constraintKey))[0] || null;
  const sameSchedule = (left, right) => backupScheduleFields.every(key => left?.[key] === right?.[key]);
  const scheduleValue = value => {
    if (!validBackupSchedule(value) || !Number.isSafeInteger(value.revision) || value.revision < 0) throw new Error('备份设置无法读取');
    return value;
  };
  useEffect(() => {
    const changed = () => setEpoch(old => old + 1);
    window.addEventListener('chriptmas-user-space', changed); window.addEventListener('chriptmas-device-required', changed);
    return () => { window.removeEventListener('chriptmas-user-space', changed); window.removeEventListener('chriptmas-device-required', changed); };
  }, []);
  useEffect(() => {
    setPresentationScope(scope);
    edit.current += 1; operation.current = null; setText(''); setPreview(null); setSaved(''); setWorking(false); setError('');
    if (!isDeviceIdentity(actor) || userStorageKey('quick-settings') !== ownerKey) { updatePending(null); return; }
    try {
      const command = readBackupCommands(commandKey, 'schedule', legacyKey)[0];
      updatePending(readConstraintPending() || (command ? { kind: 'schedule', command, session: null } : null));
    } catch (failure) { updatePending(null); setError(failure.message); }
  }, [scope]);

  async function makePreview() {
    if (busy || working || operation.current || pendingRef.current) return;
    const session = edit.current, input = text, token = gate.issue('preview', scope);
    if (!token || !isDeviceIdentity(actor)) return;
    setError(''); setSaved(''); setPreview(null); setWorking(true);
    try {
      let change = parseQuickAddition(input) || changeOf(input, value, projects);
      if (change?.kind === 'constraint') {
        const prior = readConstraintPending();
        if (prior) { updatePending(prior); throw new Error('上次约束保存待核对'); }
        const actualProjects = await api.projects();
        if (!current(token) || edit.current !== session) return;
        if (!Array.isArray(actualProjects.items)) throw new Error('项目无法读取');
        change = parseQuickAddition(input, actualProjects.items);
        if (change?.kind === 'constraint') {
          const list = await recognitionApi.loadConstraints({ projectId: change.project.id });
          if (!current(token) || edit.current !== session) return;
          if (!Array.isArray(list.items)) throw new Error('项目约束无法读取');
          change = { ...change, id: `constraint-${crypto.randomUUID()}`, label: `${change.project.name} · 添加约束`,
            from: `${list.items.length} 条`, to: change.content, display: change.content };
        }
      } else if (change?.kind === 'schedule') {
        const command = readBackupCommands(commandKey, 'schedule', legacyKey)[0];
        if (command) { updatePending({ kind: 'schedule', command, session: null }); throw new Error('上次备份设置待核对'); }
        const actual = scheduleValue(await loadBackupSchedule());
        if (!current(token) || edit.current !== session) return;
        const next = weeklySchedule(change, actual);
        change = { ...change, settings: next, revision: actual.revision, label: '自动备份',
          from: scheduleLabel(actual), to: scheduleLabel(next), display: scheduleLabel(next) };
      }
      const next = buildQuickPreview(change, session);
      if (current(token) && edit.current === session) setPreview(next);
    } catch (failure) { if (current(token) && edit.current === session) setError(failure.message || '读取未完成 · 重试'); }
    finally { if (current(token)) setWorking(false); }
  }

  function complete(change) {
    updatePending(null);
    if (change.kind === 'schedule') onBackupScheduleChange?.();
    if (edit.current === change.session) { setText(''); setPreview(null); setSaved('已应用'); }
    else setSaved('上一项已应用');
  }

  async function saveAdditional(change, token, recovering = false) {
    if (change.kind === 'constraint') {
      if (recovering) {
        const list = await recognitionApi.loadConstraints({ projectId: change.project.id });
        if (!current(token)) return;
        if (!Array.isArray(list.items)) throw new Error('项目约束无法读取');
        const row = list.items.find(item => item.id === change.id);
        if (row) {
          if (!sameConstraint(row, change)) throw Object.assign(new Error('约束内容或修订已变化 · 请在项目设置中核对'), { status: 409 });
          clearSharedCommand(change.command);
          complete(change); return;
        }
      }
      const actualProjects = await api.projects();
      if (!current(token)) return;
      if (!Array.isArray(actualProjects.items)) throw new Error('项目无法读取');
      if (!actualProjects.items.some(project => project.id === change.project.id && project.name === change.project.name)) throw conflict();
      let command = change.command;
      if (!command) {
        const prior = readConstraintPending();
        if (prior) { updatePending(prior); throw new Error('上次约束保存待核对'); }
        command = createConstraintCommand(change, constraintKey, new Date().toISOString());
      }
      if (!current(token)) return;
      command = saveSharedCommand({ ...command, phase: 'sending' });
      constraintFromCommand(command, constraintKey);
      updatePending({ ...change, command });
      const body = command.body;
      const row = await recognitionApi.saveConstraint({ projectId: body.project_id, constraintId: command.id,
        expectedRevision: body.expected_revision, content: body.content, enabled: body.enabled,
        validFrom: body.valid_from, validUntil: body.valid_until });
      if (!current(token)) return;
      if (!sameConstraint(row, change)) throw new Error('约束保存结果待核对');
      clearSharedCommand(command);
      complete(change); return;
    }
    let command = change.command;
    if (!command) {
      const prior = readBackupCommands(commandKey, 'schedule', legacyKey)[0];
      if (prior) { updatePending({ kind: 'schedule', command: prior, session: null }); throw new Error('上次备份设置待核对'); }
      const actual = scheduleValue(await loadBackupSchedule());
      if (!current(token)) return;
      if (actual.revision !== change.revision) throw conflict();
      const id = newBackupCommand();
      command = createBackupCommand('schedule', commandKey, id, { ...change.settings, expected_revision: change.revision, command_id: id });
    }
    if (recovering) {
      let receipt;
      try { receipt = await loadBackupScheduleCommand({ commandId: command.id }); }
      catch (failure) { if (failure.status !== 404 || failure.code !== 'backup_command_not_found') throw failure; }
      if (!current(token)) return;
      if (receipt) {
        if (receipt.command_id !== command.id || receipt.state !== 'saved' || !sameSchedule(receipt.settings, command.body)
            || receipt.expected_revision !== command.body.expected_revision || !Number.isSafeInteger(receipt.revision) || receipt.revision < 1) throw new Error('备份保存结果无法核对');
        settleBackupCommand(command); complete(change); return;
      }
      const actual = scheduleValue(await loadBackupSchedule());
      if (!current(token)) return;
      if (actual.revision !== command.body.expected_revision) {
        if (actual.revision > command.body.expected_revision) {
          settleBackupCommand(command); updatePending(null); onBackupScheduleChange?.();
        }
        throw conflict();
      }
    }
    if (!current(token)) return;
    command = saveBackupCommand(command, 'sending');
    updatePending({ ...change, command });
    onBackupScheduleChange?.();
    const receipt = await saveBackupSchedule(command.body);
    if (!current(token)) return;
    if (receipt.command_id !== command.id || receipt.state !== 'saved' || !sameSchedule(receipt.settings, command.body)
        || receipt.expected_revision !== command.body.expected_revision || !Number.isSafeInteger(receipt.revision) || receipt.revision < 1) throw new Error('备份保存结果待核对');
    settleBackupCommand(command); complete(change);
  }

  async function recover() {
    const change = pendingRef.current;
    if (!change || busy || working || operation.current) return;
    const token = gate.issue('apply', scope);
    if (!token || !isDeviceIdentity(actor)) return;
    operation.current = token; setWorking(true); setError('');
    await run(async () => {
      try { await saveAdditional(change, token, true); }
      catch (failure) { if (current(token)) setError(failure.message || (failure.status === 409 ? '设置已变化 · 重新预览' : '结果未知 · 核对并重试')); }
      finally { if (operation.current === token) operation.current = null; if (current(token)) setWorking(false); }
    });
  }
  async function apply() {
    const change = preview;
    if (!change || change.error || busy || working || operation.current || pendingRef.current || change.session !== edit.current) return;
    const token = gate.issue('apply', scope);
    if (!token || !isDeviceIdentity(actor)) return;
    operation.current = token; setWorking(true); setError('');
    await run(async () => {
      try {
      if (change.kind === 'constraint' || change.kind === 'schedule') { await saveAdditional(change, token); return; }
      if (change.kind === 'theme') {
        if (readStoredTheme() !== change.from) throw conflict();
        applyTheme(change.to);
      } else {
        const fresh = await api.load();
        if (!current(token)) return;
        if (change.kind === 'privacy') {
          if (fresh.privacy.revision !== change.revision) throw conflict();
          const ids = fresh.privacy.private_projects.filter(id => id !== change.project.id);
          if (change.to) ids.push(change.project.id);
          await api.savePrivacy(ids, change.revision);
        } else if (change.kind === 'agent') {
          const row = fresh.external_agent;
          if (row.revision !== change.revision) throw conflict();
          await api.saveExternalAgent({ allow_remote: change.to, include_profile: row.include_profile, daily_limit: row.daily_limit, clients: { ...row.clients }, expected_revision: row.revision });
        } else if (change.kind === 'mode') {
          if ((fresh.model[change.purpose].mode_revision ?? 0) !== change.revision) throw conflict();
          const args = { mode: change.to, expectedRevision: change.revision };
          await (change.purpose === 'vision' ? api.saveVisionMode(args) : api.saveEmbeddingMode(args));
        } else {
          const row = fresh.model[change.purpose];
          if (row.revision !== change.revision || change.purpose === 'generation' && fresh.model.generation_mode.mode !== 'api' || ['vision', 'embedding'].includes(change.purpose) && row.mode === 'local') throw conflict();
          await api.saveModel({ purpose: change.purpose, serverManaged: row.server_managed === true, baseUrl: row.base_url, model: row.model, apiKey: '', allowRemote: change.to, enabled: change.to || row.enabled === true, expectedRevision: row.revision });
        }
      }
      if (current(token)) complete(change);
      } catch (failure) { if (current(token)) setError(failure.message || (failure.status === 409 ? '设置已变化 · 重新预览' : '操作未完成 · 重试')); }
      finally { if (operation.current === token) operation.current = null; if (current(token)) setWorking(false); }
    });
  }
  return <section className="settings-quick" aria-label="一句话改设置">
    <form className="settings-quick-input" onSubmit={event => { event.preventDefault(); makePreview(); }}>
      <Icon name="pattern" size={18}/><input aria-label="一句话改设置" placeholder="说一句，比如：每周二 09:00 备份" value={visible ? text : ''} disabled={!visible} onChange={event => { edit.current += 1; gate.invalidate('preview'); setText(event.target.value); setPreview(null); setSaved(''); if (!operation.current) setWorking(false); }}/>
      {visible && saved && <span role="status">{saved}</span>}<button type="submit" disabled={!visible || busy || working || Boolean(pending) || !text.trim()}>预览</button>
    </form>
    {visible && error && <p role="alert" className="settings-quick-help">{error}</p>}
    {visible && pending && <div className="settings-quick-diff" role="status"><span>待核对</span><strong style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', minWidth: 0 }}>{pending.kind === 'constraint' ? `${pending.project.name} · ${pending.content}` : scheduleLabel(pending.command.body)}</strong><button type="button" disabled={busy || working} onClick={recover}>核对并重试</button></div>}
    {visible && (preview?.error ? <p role="status" className="settings-quick-help">{preview.error}</p> : preview && <div className="settings-quick-diff" role="region" aria-label="设置变更预览"><span>{preview.label}</span><span className="settings-quick-old">{stateLabel(preview.from)}</span><span aria-hidden="true">→</span><strong style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', minWidth: 0 }}>{preview.display || stateLabel(preview.to)}</strong><span className="settings-quick-actions"><button type="button" disabled={busy || working || Boolean(pending)} onClick={() => setPreview(null)}>取消</button><button type="button" disabled={busy || working || Boolean(pending) || preview.kind !== 'constraint' && preview.from === preview.to} onClick={apply}>应用</button></span></div>)}
  </section>;
}
