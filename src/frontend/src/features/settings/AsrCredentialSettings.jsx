import { useEffect, useRef, useState } from 'react';
import { Row } from '../../shared/ui';
import { userStorageKey } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { clearSharedCommand, readSharedCommands, saveSharedCommand } from '../shared-projects/sharedProjectCommands';

const storageScope = () => userStorageKey('asr-key-operations');
const baselineOf = row => ({ settings_revision: row.settings_revision ?? 0, secret_generation: row.secret_generation ?? 0 });
const same = (left, right) => left.settings_revision === right.settings_revision && left.secret_generation === right.secret_generation;
const statusText = { stored: '已保存', cancelled: '未受理并已取消', unknown: '保存结果未知', not_recorded: '尚未读取到受理记录' };

export function AsrCredentialSettings({ row, api, onSaved }) {
  const [userSpace, setUserSpace] = useState(storageScope), [key, setKey] = useState('');
  const gate = useRequestScope(userSpace), scope = gate.scope;
  const [baseline, setBaseline] = useState(() => baselineOf(row)), [rechecked, setRechecked] = useState(false);
  const [commands, setCommands] = useState([]), [results, setResults] = useState({}), [error, setError] = useState('');
  const [busy, setBusy] = useState(false), [saved, setSaved] = useState(false), [ready, setReady] = useState(false);
  const immediate = useRef(false), controller = useRef(null), ownCommand = useRef(null), edited = useRef(false);
  const draftVersion = useRef(0), submittedVersion = useRef(null);
  const current = token => storageScope() === userSpace && gate.isCurrent(token);
  const changed = !same(baseline, baselineOf(row));
  const blocked = commands.length > 0 && !rechecked;
  function loadCommands() {
    const values = readSharedCommands(userSpace);
    if (values.some(value => value.path !== 'asr-key' || value.body.id !== value.id
      || Object.keys(value.body).some(field => !['id', 'kind'].includes(field)) || value.body.kind !== 'asr-key')) throw new Error('操作记录无法核对 · 保留浏览器资料');
    setCommands(values); setReady(true); return values;
  }
  useEffect(() => {
    const changedSpace = () => { setKey(''); ownCommand.current = null; setUserSpace(storageScope()); };
    window.addEventListener('chriptmas-user-space', changedSpace); window.addEventListener('chriptmas-device-required', changedSpace);
    return () => { window.removeEventListener('chriptmas-user-space', changedSpace); window.removeEventListener('chriptmas-device-required', changedSpace); };
  }, []);
  useEffect(() => {
    setKey(''); setResults({}); setCommands([]); setError(''); setReady(false); setSaved(false); setBusy(false); setRechecked(false);
    setBaseline(baselineOf(row)); ownCommand.current = null; edited.current = false; immediate.current = false;
    try { loadCommands(); } catch (reason) { setError(reason.message); }
    const stored = event => { if (!event.key || event.key.startsWith(userSpace + ':operation:')) { try { loadCommands(); } catch (reason) { setReady(false); setError(reason.message); } } };
    window.addEventListener('storage', stored);
    return () => { window.removeEventListener('storage', stored); controller.current?.abort(); gate.invalidate('key'); };
  }, [scope]);
  useEffect(() => { if (!edited.current && commands.length === 0) setBaseline(baselineOf(row)); }, [row.settings_revision, row.secret_generation, commands.length]);
  function accept(command, result) {
    if (result?.id !== command.id || !Object.hasOwn(statusText, result.status) || !Number.isInteger(result.current?.secret_generation)
      || !Number.isInteger(result.current?.settings_revision)) throw new Error('凭据状态无法核对');
    setResults(values => ({ ...values, [command.id]: result }));
    if (['stored', 'cancelled'].includes(result.status)) {
      clearSharedCommand(command);
      if (result.status === 'stored' && ownCommand.current === command.id) {
        if (draftVersion.current === submittedVersion.current) {
          setKey(''); setSaved(true); edited.current = false; setBaseline(result.current);
        }
        ownCommand.current = null; onSaved?.();
      }
    } else saveSharedCommand({ ...command, phase: result.status === 'not_recorded' ? 'not_received' : 'unknown' });
    loadCommands();
  }
  async function read(command, cancel = false) {
    if (immediate.current || busy) return;
    const token = gate.issue('key', scope); if (!token || !current(token)) return;
    const request = new AbortController(); controller.current = request; immediate.current = true; setBusy(true); setError('');
    try { const result = await api[cancel ? 'cancelAsrKey' : 'asrKeyStatus'](command.id, request.signal); if (current(token)) accept(command, result); }
    catch (reason) { if (current(token)) setError(reason.message || '保存状态未读取 · 重试'); }
    finally { if (current(token)) { immediate.current = false; setBusy(false); } }
  }
  async function verify() {
    if (busy || immediate.current) return;
    const token = gate.issue('key', scope); if (!token || !current(token)) return;
    immediate.current = true; setBusy(true); setError('');
    try {
      const settings = await api.load(), fresh = settings.model.asr;
      if (!current(token)) return;
      if (fresh.server_managed) throw new Error('服务器共享转写 · 由管理员配置密钥');
      const outstanding = loadCommands();
      if (outstanding.some(command => !results[command.id] || results[command.id].status === 'not_recorded')) {
        setError('先读取待核对操作；未受理时确认取消'); return;
      }
      setBaseline(baselineOf(fresh)); setRechecked(true); ownCommand.current = null; onSaved?.();
    } catch (reason) { if (current(token)) setError(reason.message || '配置未核对 · 重试'); }
    finally { if (current(token)) { immediate.current = false; setBusy(false); } }
  }
  async function save() {
    if (busy || immediate.current || !ready || blocked || changed || row.server_managed || !key.trim()) return;
    const token = gate.issue('key', scope); if (!token || !current(token)) return;
    const request = new AbortController(); controller.current = request; immediate.current = true; setBusy(true); setError(''); setSaved(false);
    const identity = `cmd-${crypto.randomUUID()}`, command = { version: 1, storageKey: userSpace, id: identity, path: 'asr-key',
      body: { id: identity, kind: 'asr-key' }, expected: baseline, phase: 'prepared', createdAt: new Date().toISOString() };
    let recorded = false;
    try {
      saveSharedCommand(command); recorded = true; loadCommands(); ownCommand.current = identity;
      submittedVersion.current = draftVersion.current; setRechecked(false);
      saveSharedCommand({ ...command, phase: 'sending' });
      const result = await api.saveWebAsrKey({ api_key: key, command_id: identity, expected_settings_revision: baseline.settings_revision,
        expected_generation: baseline.secret_generation }, request.signal);
      if (current(token)) accept(command, result);
    } catch (reason) {
      if (!current(token)) return;
      if (recorded) {
        try { saveSharedCommand({ ...command, phase: 'unknown' }); loadCommands(); } catch (failure) { setReady(false); setError(failure.message); return; }
      }
      setError(!recorded ? '操作记录未保存 · 输入保留' : reason.status === 409 ? '凭据或设置已变化 · 读取本次状态后核对' : '保存结果待核对 · 读取本次状态');
    } finally { if (current(token)) { immediate.current = false; setBusy(false); } }
  }
  return <section aria-label="Hy-ASR 凭据" aria-busy={busy}>
    <div className="settings-fields"><label>地址<span className="ui-row-meta">{row.endpoint || '—'}</span></label>
      <label>模型<span className="ui-row-meta">{row.model || '—'}</span></label>
      <label>密钥<input type="password" aria-label="转写密钥" autoComplete="new-password" value={key}
        placeholder={row.has_api_key ? '已设置 · 可替换' : '未设置'} disabled={busy || row.server_managed}
        onChange={event => { draftVersion.current += 1; edited.current = true; setKey(event.target.value); setSaved(false); }}/></label></div>
    {row.server_managed ? <p className="settings-login-hint">服务器管理 · API 与单价由管理员配置</p> : <div className="settings-support-actions">
      <button type="button" disabled={busy || !ready || !key.trim() || changed || blocked} title={`设置 r${baseline.settings_revision} · 凭据代次 ${baseline.secret_generation}`} onClick={save}>{saved ? '✓ 已保存' : '保存密钥'}</button>
      {(changed || commands.length > 0) && <button type="button" disabled={busy} onClick={verify}>核对后重新提交</button>}
    </div>}
    {commands.map(command => <Row key={command.id} readOnly title={statusText[results[command.id]?.status] || '待核对保存'}
      sub={command.id} trailing={<><button type="button" disabled={busy} onClick={() => read(command)}>读取状态</button>
        {results[command.id]?.status === 'not_recorded' && <button type="button" disabled={busy} onClick={() => read(command, true)}>取消未受理操作</button>}</>}/>) }
    {error && <p role="alert">{error}</p>}
  </section>;
}
