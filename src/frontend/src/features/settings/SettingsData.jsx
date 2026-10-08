import { useEffect, useRef, useState } from 'react';
import { Row, Switch } from '../../shared/ui';
import { listMemorySnapshots, createMemorySnapshot } from '../rebuild/localMemoryVaultApi';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { invalidateSignals, setSignalsEnabled, signalEpoch } from '../../shared/signalsApi';
import { devicesApi } from './devicesApi';
import { settingsApi } from './settingsApi';
export function SettingsData({ projectId, loadSnapshots = listMemorySnapshots, createSnapshot = createMemorySnapshot, checkIntegrity = settingsApi.integrity }) {
  const [open, setOpen] = useState(false), [snapshots, setSnapshots] = useState(null), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const gate = useRequestScope(projectId), scope = gate.scope;
  const [integrity, setIntegrity] = useState(null), [checking, setChecking] = useState(false);
  const [integrityOpen, setIntegrityOpen] = useState(false), [integrityError, setIntegrityError] = useState(false);
  useEffect(() => { setIntegrity(null); setChecking(false); setIntegrityOpen(false); setIntegrityError(false); }, [projectId]);
  const [signals, setSignals] = useState(null), [signalBusy, setSignalBusy] = useState(false), [signalError, setSignalError] = useState(false), [armed, setArmed] = useState(false), [server, setServer] = useState(false);
  const confirmTimer = useRef(null);
  useEffect(() => {
    const token = gate.issue('signals-load', scope);
    setSignals(null); setSignalBusy(false); setSignalError(false); setArmed(false); setServer(false);
    settingsApi.signals().then(value => { if (gate.isCurrent(token) && typeof value.enabled === 'boolean' && Number.isInteger(value.revision)) { setSignals(value); setSignalsEnabled(value.enabled); } }).catch(() => { if (gate.isCurrent(token)) setSignalError(true); });
    devicesApi.list().then(value => { if (gate.isCurrent(token)) setServer(value.mode === 'server'); }).catch(() => {});
    return () => clearTimeout(confirmTimer.current);
  }, [projectId]);
  async function reloadSignals() {
    if (signalBusy) return;
    const token = gate.issue('signals-write', scope); if (!token) return;
    const operationEpoch = signalEpoch();
    setSignalBusy(true); setArmed(false); clearTimeout(confirmTimer.current);
    try { const value = await settingsApi.signals(); if (gate.isCurrent(token)) { setSignals(value); setSignalError(false); if (signalEpoch() === operationEpoch) setSignalsEnabled(value.enabled); } }
    catch { if (gate.isCurrent(token)) setSignalError(true); }
    finally { if (gate.isCurrent(token)) setSignalBusy(false); }
  }
  async function changeSignals(enabled) {
    if (!signals || signalBusy) return;
    const token = gate.issue('signals-write', scope); if (!token) return;
    setSignalBusy(true); setSignalError(false);
    invalidateSignals(enabled ? signals.enabled : false);
    const operationEpoch = signalEpoch();
    try { const value = await settingsApi.saveSignals(enabled, signals.revision); if (signalEpoch() === operationEpoch) setSignalsEnabled(value.enabled); if (gate.isCurrent(token)) setSignals(value); }
    catch { if (signalEpoch() === operationEpoch) setSignalsEnabled(signals.enabled); if (gate.isCurrent(token)) setSignalError(true); }
    finally { if (gate.isCurrent(token)) setSignalBusy(false); }
  }
  async function clearSignals() {
    if (!signals || signalBusy) return;
    if (!armed) { setArmed(true); confirmTimer.current = setTimeout(() => setArmed(false), 3000); return; }
    clearTimeout(confirmTimer.current); setArmed(false);
    const token = gate.issue('signals-write', scope); if (!token) return;
    setSignalBusy(true); setSignalError(false); invalidateSignals();
    const operationEpoch = signalEpoch();
    try { await settingsApi.clearSignals(signals.revision); const value = await settingsApi.signals(); if (signalEpoch() === operationEpoch) setSignalsEnabled(value.enabled); if (gate.isCurrent(token)) setSignals(value); }
    catch { if (gate.isCurrent(token)) setSignalError(true); }
    finally { if (gate.isCurrent(token)) setSignalBusy(false); }
  }
  async function check() {
    if (checking) return;
    const token = gate.issue('integrity', scope); if (!token) return;
    if (integrity) setIntegrityOpen(value => !value);
    setChecking(true); setIntegrityError(false);
    try {
      const value = await checkIntegrity();
      if (gate.isCurrent(token)) setIntegrity(value);
    } catch { if (gate.isCurrent(token)) setIntegrityError(true); }
    finally { if (gate.isCurrent(token)) setChecking(false); }
  }
  useEffect(() => {
    if (!open) return;
    const token = gate.issue('snapshots', scope);
    loadSnapshots().then(value => { if (gate.isCurrent(token)) setSnapshots(value.snapshots || []); })
      .catch(() => { if (gate.isCurrent(token)) setError('backup_read_failed'); });
  }, [open, projectId, loadSnapshots]);
  async function backup() {
    const token = gate.issue('backup', scope); if (!token || busy) return;
    setBusy(true); setError('');
    try { await createSnapshot(); const value = await loadSnapshots(); if (gate.isCurrent(token)) setSnapshots(value.snapshots || []); }
    catch (failure) { if (gate.isCurrent(token)) setError(['backup_source_changed', 'backup_sqlite_busy', 'backup_sqlite_failed', 'backup_failed', 'server_unavailable', 'invalid_response', 'network_error'].includes(failure?.code) ? failure.code : 'backup_failed'); }
    finally { if (gate.isCurrent(token)) setBusy(false); }
  }
  return <>
    <Row title="备份" expanded={open} meta={snapshots ? String(snapshots.length) : '—'} trailing={error && <span role="alert">✕ {error}</span>} onOpen={() => setOpen(value => !value)}/>
    {open && <div className="settings-expand">{(snapshots || []).map(row => <Row readOnly key={row.snapshot_id} title={row.created_at || '—'} meta={<>{Number.isFinite(row.size_bytes) && row.size_bytes >= 0 ? `${row.size_bytes} B` : '—'}{typeof row.verified === 'boolean' && <span className={`settings-backup-verification${row.verified ? '' : ' is-failed'}`} title={row.verified ? undefined : 'backup_verification_failed'}>{row.verified ? '✓' : '✕'}</span>}</>}/>)}<button type="button" disabled={busy} onClick={backup}>立即备份</button></div>}
    <Row title="检查" expanded={integrityOpen} onOpen={check}
      meta={checking ? '…' : integrity ? integrity.ok ? '✓' : `✕ ${integrity.problems.reduce((count, problem) => count + problem.count, 0)}` : null}
      trailing={integrityError && <span role="alert">重试</span>}/>
    {integrityOpen && integrity && <div className="settings-expand">{integrity.problems.map(problem =>
      <Row key={problem.code} title={problem.code} meta={String(problem.count)} readOnly className="settings-integrity-reason"/>)}</div>}
    <Row readOnly title="使用记录" className="settings-signals-row" meta={<span className="settings-signals-controls" title={`保留 180 天${server ? ' · 管理员可见' : ''}`}><span>{signals ? `${signals.count} 条` : '— 条'}</span><button type="button" className={armed ? 'settings-signals-clear is-confirming' : 'settings-signals-clear'} aria-label={signalError ? '重试读取使用记录' : armed ? '确认清除使用记录' : '清除使用记录'} title={signalError ? 'signal_settings_failed' : undefined} disabled={signalBusy || !signals && !signalError} onClick={signalError ? reloadSignals : clearSignals}>{signalError ? '重试' : armed ? '确认' : '清除'}</button><Switch label="使用记录" checked={signals?.enabled ?? false} disabled={!signals || signalBusy} onChange={changeSignals}/></span>}/>
  </>;
}
