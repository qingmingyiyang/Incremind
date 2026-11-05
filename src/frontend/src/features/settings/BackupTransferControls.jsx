import { useEffect, useRef, useState } from 'react';
import { deviceIdentity, isDeviceIdentity, userStorageKey } from '../../shared/api/deviceTransport';
import { formatBytes } from '../../shared/lib/formatBytes';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { cancelUnreceivedBackupImport, downloadMemorySnapshot, importMemorySnapshot, loadBackupImport, newBackupCommand } from '../rebuild/localMemoryVaultApi';
import { canResendBackupFile, createBackupCommand, isBackupCommandSettled, readBackupCommands, saveBackupCommand, settleBackupCommand } from './backupCommands';

const storageKey = name => userStorageKey(`${name}:${globalThis.electronAPI?.backendBaseUrl || globalThis.location?.origin || ''}`);

export function BackupImportButton({ scopeKey, blocked, onImported, onBusyChange }) {
  const key = storageKey('backup-import-operations'), legacyKey = storageKey('backup-import-command');
  const [initial] = useState(() => {
    try { return { commands: readBackupCommands(key, 'import', legacyKey), error: '' }; }
    catch (failure) { return { commands: [], error: failure.message }; }
  });
  const [pending, setPendingState] = useState(initial.commands[0] || null), pendingRef = useRef(pending);
  const [phase, setPhase] = useState(pending ? 'checking' : 'idle'), [error, setError] = useState('');
  const [journalError, setJournalError] = useState(initial.error), [count, setCount] = useState(initial.commands.length);
  const [readEpoch, setReadEpoch] = useState(0);
  const input = useRef(null), file = useRef(null), request = useRef(null), phaseRef = useRef(phase);
  const gate = useRequestScope(scopeKey), scope = gate.scope;
  const busy = ['uploading', 'checking', 'finishing', 'cancelling'].includes(phase);
  function setPending(value) { pendingRef.current = value; setPendingState(value); }
  function changePhase(value) { phaseRef.current = value; setPhase(value); }
  function advance(terminal = 'idle', failure = '') {
    try {
      const commands = readBackupCommands(key, 'import', legacyKey);
      setCount(commands.length); setPending(commands[0] || null); setJournalError('');
      changePhase(commands.length ? 'checking' : terminal); setError(commands.length ? '' : failure);
    } catch (reason) { setJournalError(reason.message); changePhase('checking'); throw reason; }
  }
  function syncJournal(restart = false) {
    try {
      const commands = readBackupCommands(key, 'import', legacyKey);
      setJournalError(''); setCount(commands.length);
      if (phaseRef.current === 'finishing') return;
      if (commands.some(value => value.id === pendingRef.current?.id)) {
        if (restart) { changePhase('checking'); setReadEpoch(value => value + 1); }
        return;
      }
      if (!pendingRef.current && commands.length === 0) return;
      if (pendingRef.current && !isBackupCommandSettled(pendingRef.current)) throw new Error('当前导入记录已变化，请保留当前页面');
      gate.invalidate('import-write'); gate.invalidate('import-read'); request.current?.abort();
      file.current = null; if (input.current) input.current.value = '';
      setPending(commands[0] || null); changePhase(commands.length ? 'checking' : 'idle'); setError('');
    } catch (failure) { setJournalError(failure.message); }
  }

  useEffect(() => { onBusyChange(busy); return () => onBusyChange(false); }, [busy, onBusyChange]);
  useEffect(() => () => { request.current?.abort(); file.current = null; }, []);
  useEffect(() => {
    const changed = event => { if (event.key === null || event.key === legacyKey || event.key?.startsWith(`${key}:operation:`)) syncJournal(); };
    globalThis.window?.addEventListener('storage', changed);
    return () => globalThis.window?.removeEventListener('storage', changed);
  }, [key, legacyKey, scope]);

  async function received(value, command) {
    if (value?.command_id !== command.id) throw new Error('backup_command_changed');
    if (pendingRef.current?.id !== command.id) return true;
    if (value.state === 'published' && typeof value.snapshot_id === 'string') {
      settleBackupCommand(command); changePhase('finishing'); setError('');
      file.current = null; if (input.current) input.current.value = '';
      const token = gate.issue('import-selection', scope), device = deviceIdentity();
      try { await onImported(value.snapshot_id); }
      finally { if (gate.isCurrent(token) && isDeviceIdentity(device) && pendingRef.current?.id === command.id) advance('complete'); }
      return true;
    }
    if (['failed', 'cancelled'].includes(value.state)) {
      settleBackupCommand(command);
      file.current = null; if (input.current) input.current.value = '';
      advance(value.state, value.state === 'failed' ? value.reason_code || 'backup_import_failed' : '');
      return true;
    }
    if (!['receiving', 'uploaded', 'publishing'].includes(value.state)) throw new Error('backup_import_result_invalid');
    saveBackupCommand(command, 'pending'); changePhase('checking'); return false;
  }

  useEffect(() => {
    if (!pending || phase !== 'checking') return;
    const token = gate.issue('import-read', scope), device = deviceIdentity();
    let timer, controller, disposed = false;
    const current = () => !disposed && gate.isCurrent(token) && isDeviceIdentity(device) && pendingRef.current?.id === pending.id;
    const poll = async () => {
      controller = new AbortController();
      try {
        const value = await loadBackupImport({ commandId: pending.id, signal: controller.signal });
        if (!current()) return;
        setError(''); if (await received(value, pending)) return;
      } catch (failure) {
        if (!current()) return;
        if (failure.status === 404 && failure.code === 'backup_command_not_found') {
          try { saveBackupCommand(pending, 'not_received'); changePhase('not_accepted'); setError(''); }
          catch (reason) { setJournalError(reason.message); }
          return;
        }
        setError('backup_import_read_failed');
      }
      if (current()) timer = setTimeout(poll, 2000);
    };
    poll();
    return () => { disposed = true; clearTimeout(timer); controller?.abort(); };
  }, [pending, phase, scope, readEpoch]);

  async function cancelPending() {
    const command = pendingRef.current;
    if (!command) return true;
    const token = gate.issue('import-write', scope), device = deviceIdentity(), controller = new AbortController();
    const current = () => gate.isCurrent(token) && isDeviceIdentity(device) && pendingRef.current?.id === command.id;
    try {
      saveBackupCommand(command, 'unknown'); request.current = controller;
      changePhase('cancelling'); setError('');
      const result = await cancelUnreceivedBackupImport({ commandId: command.id, signal: controller.signal });
      if (!current()) return false;
      await received(result, command);
      return result.state === 'cancelled' && pendingRef.current === null;
    } catch (failure) {
      if (current()) { setError(failure.message || '取消结果暂不可读'); changePhase('checking'); }
      return false;
    } finally { if (request.current === controller) request.current = null; }
  }

  async function chooseFile() {
    if (blocked || busy || journalError) return;
    if (pendingRef.current) { await cancelPending(); return; }
    if (input.current) { input.current.value = ''; input.current.click(); }
  }

  async function upload(selected) {
    if (blocked || busy || journalError || !selected) return;
    let command = pendingRef.current;
    if (command && !canResendBackupFile(command, selected, file.current)) {
      if (!await cancelPending()) return;
      command = null;
    }
    if (!command) command = createBackupCommand('import', key, newBackupCommand(), {
      file: { name: selected.name || '', size: selected.size, type: selected.type, lastModified: selected.lastModified ?? null },
    });
    let pendingCount;
    try { command = saveBackupCommand(command, 'sending'); pendingCount = readBackupCommands(key, 'import', legacyKey).length; }
    catch (failure) { setJournalError(failure.message); return; }
    const token = gate.issue('import-write', scope), device = deviceIdentity(), controller = new AbortController();
    request.current = controller; file.current = { id: command.id, file: selected };
    setPending(command); setCount(pendingCount); changePhase('uploading'); setError('');
    const current = () => gate.isCurrent(token) && isDeviceIdentity(device) && request.current === controller && pendingRef.current?.id === command.id;
    try {
      const result = await importMemorySnapshot({ commandId: command.id, file: selected, signal: controller.signal });
      if (current()) await received(result, command);
    } catch (failure) {
      if (current()) { setError(failure.code || 'backup_import_read_failed'); changePhase('checking'); }
    }
    finally { if (request.current === controller) request.current = null; }
  }

  return <>
    <input ref={input} type="file" accept=".zip,application/zip" aria-label="选择备份归档" hidden
      onChange={event => upload(event.target.files?.[0])}/>
    <button type="button" className="settings-support-button" disabled={blocked || busy || Boolean(journalError)}
      onClick={() => phase === 'not_accepted' && file.current?.id === pending?.id ? upload(file.current.file) : chooseFile()}>
      {phase === 'uploading' ? '导入中' : phase === 'cancelling' ? '取消核对中' : ['checking', 'finishing'].includes(phase) ? '核对中'
        : phase === 'not_accepted' ? file.current?.id === pending?.id ? '重试本次导入' : '取消本次导入' : '导入'}</button>
    {phase === 'complete' && <span role="status">✓ 已导入</span>}
    {phase === 'not_accepted' && <span role="status">此次导入尚未受理</span>}
    {phase === 'not_accepted' && file.current?.id === pending?.id && <button type="button" className="settings-support-button" disabled={blocked || Boolean(journalError)}
      onClick={chooseFile}>取消本次导入</button>}
    {phase === 'cancelled' && <span role="status">已取消，可选择新文件</span>}
    {count > 1 && <span role="status">待核对 {count} 项</span>}
    {journalError && <span role="alert">{journalError}<button type="button" onClick={() => syncJournal(true)}>重新读取操作记录</button></span>}
    {error && <span role="alert" title={error}>{phase === 'checking' ? '结果暂不可读' : '导入未完成'}
      {phase === 'checking' && <button type="button" onClick={() => setReadEpoch(value => value + 1)}>核对结果</button>}</span>}
  </>;
}

export function BackupDownloadButton({ snapshotId, scopeKey, blocked }) {
  const [busy, setBusy] = useState(false), [error, setError] = useState(false), [size, setSize] = useState(null);
  const pending = useRef(null), gate = useRequestScope(`${scopeKey}:${snapshotId}`), scope = gate.scope;
  useEffect(() => { setError(false); setBusy(false); setSize(null); return () => pending.current?.abort(); }, [scope]);
  async function download() {
    if (blocked || busy) return;
    const token = gate.issue('download', scope), identity = deviceIdentity(), controller = new AbortController();
    pending.current = controller; setBusy(true); setError(false); setSize(null);
    const current = () => gate.isCurrent(token) && isDeviceIdentity(identity) && !controller.signal.aborted;
    try {
      const result = await downloadMemorySnapshot({ snapshotId, signal: controller.signal, isCurrent: current });
      if (current() && result) setSize(result.size_bytes);
    } catch { if (current()) setError(true); }
    finally { if (current()) setBusy(false); if (pending.current === controller) pending.current = null; }
  }
  return <><button type="button" className="settings-support-button" disabled={blocked || busy || !snapshotId}
    onClick={download}>{busy ? '下载中' : error ? '重试下载' : '下载'}</button>
    {error && <span role="alert">下载未完成</span>}
    {size !== null && <span role="status">✓ {formatBytes(size)}</span>}</>;
}
