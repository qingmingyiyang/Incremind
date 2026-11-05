import { useEffect, useRef, useState } from 'react';
import { Row, Switch, Icon } from '../../shared/ui';
import { formatBytes } from '../../shared/lib/formatBytes';
import { listMemorySnapshots, createMemorySnapshot, createRollbackPlan, restoreMemorySnapshot, loadMemoryRestore, resumeMemoryRestore, releaseRestorePreview } from '../rebuild/localMemoryVaultApi';
import { compareDateValues, formatLocalShortDate } from '../../shared/lib/time';
import { deviceIdentity, isDeviceIdentity, userStorageKey } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { invalidateSignals, setSignalsEnabled, signalEpoch } from '../../shared/signalsApi';
import { devicesApi } from './devicesApi';
import { settingsApi } from './settingsApi';
import { DailyInspirationSettings } from './DailyInspirationSettings';
import { BackupDownloadButton, BackupImportButton } from './BackupTransferControls';
import { BackupScheduleSettings } from './BackupScheduleSettings';
import './SettingsSupport.css';
export function SettingsData({ projectId, scheduleRefresh = 0, loadSnapshots = listMemorySnapshots, createSnapshot = createMemorySnapshot, checkIntegrity = settingsApi.integrity }) {
  const [open, setOpen] = useState(true), [snapshots, setSnapshots] = useState(null), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const [snapshotError, setSnapshotError] = useState(false), [loadingSnapshots, setLoadingSnapshots] = useState(false);
  const [transferBusy, setTransferBusy] = useState(false);
  const [selectedSnapshot, setSelectedSnapshot] = useState(null), [restorePlan, setRestorePlan] = useState(null);
  const [restoring, setRestoring] = useState(false), [restoreError, setRestoreError] = useState(false), [restoreStatus, setRestoreStatus] = useState('');
  const [restoreOperations, setRestoreOperations] = useState([]);
  const [restoreOperationId, setRestoreOperationId] = useState(null), [restoreReadError, setRestoreReadError] = useState(false);
  const restoreAttempt = useRef(null);
  useEffect(() => {
    if (!restorePlan?.preview_id) return;
    const plan = restorePlan;
    const release = () => {
      if (restoreAttempt.current?.operation_id !== plan.operation_id)
        releaseRestorePreview({ previewId: plan.preview_id, identity: plan.identity, keepalive: true }).catch(() => {});
    };
    const timer = setTimeout(() => {
      if (restoreAttempt.current?.operation_id === plan.operation_id) return;
      release(); setRestorePlan(null); setRestoreStatus('preview_expired');
    }, Math.max(0, Date.parse(plan.preview_expires_at) - Date.now()));
    window.addEventListener('pagehide', release);
    return () => { clearTimeout(timer); window.removeEventListener('pagehide', release); release(); };
  }, [restorePlan]);
  const [identityEpoch, setIdentityEpoch] = useState(0);
  const gate = useRequestScope(`${userStorageKey(projectId)}:${identityEpoch}`), scope = gate.scope;
  const [integrity, setIntegrity] = useState(null), [checking, setChecking] = useState(false), [checkedAt, setCheckedAt] = useState(null);
  const [integrityOpen, setIntegrityOpen] = useState(false), [integrityError, setIntegrityError] = useState(false);
  useEffect(() => {
    const changed = () => {
      for (const channel of ['signals-load', 'signals-write', 'snapshots', 'backup', 'integrity', 'restore', 'restore-poll', 'import-selection']) gate.invalidate(channel);
      setIdentityEpoch(value => value + 1);
    };
    window.addEventListener('chriptmas-user-space', changed);
    window.addEventListener('chriptmas-device-required', changed);
    return () => {
      window.removeEventListener('chriptmas-user-space', changed);
      window.removeEventListener('chriptmas-device-required', changed);
    };
  }, [gate]);
  useEffect(() => { setIntegrity(null); setChecking(false); setIntegrityOpen(false); setIntegrityError(false); setCheckedAt(null); }, [scope]);
  const [signals, setSignals] = useState(null), [signalBusy, setSignalBusy] = useState(false), [signalError, setSignalError] = useState(false), [armed, setArmed] = useState(false), [server, setServer] = useState(false);
  const confirmTimer = useRef(null);
  useEffect(() => {
    const token = gate.issue('signals-load', scope);
    setSignals(null); setSignalBusy(false); setSignalError(false); setArmed(false); setServer(false);
    settingsApi.signals().then(value => { if (gate.isCurrent(token) && typeof value.enabled === 'boolean' && Number.isInteger(value.revision)) { setSignals(value); setSignalsEnabled(value.enabled); } }).catch(() => { if (gate.isCurrent(token)) setSignalError(true); });
    devicesApi.list().then(value => { if (gate.isCurrent(token)) setServer(value.mode === 'server'); }).catch(() => {});
    return () => clearTimeout(confirmTimer.current);
  }, [scope]);
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
    try { const value = await settingsApi.saveSignals(enabled, signals.revision); if (gate.isCurrent(token)) { if (signalEpoch() === operationEpoch) setSignalsEnabled(value.enabled); setSignals(value); } }
    catch { if (gate.isCurrent(token)) { if (signalEpoch() === operationEpoch) setSignalsEnabled(signals.enabled); setSignalError(true); } }
    finally { if (gate.isCurrent(token)) setSignalBusy(false); }
  }
  async function clearSignals() {
    if (!signals || signalBusy) return;
    if (!armed) { setArmed(true); confirmTimer.current = setTimeout(() => setArmed(false), 3000); return; }
    clearTimeout(confirmTimer.current); setArmed(false);
    const token = gate.issue('signals-write', scope); if (!token) return;
    setSignalBusy(true); setSignalError(false); invalidateSignals();
    const operationEpoch = signalEpoch();
    try { await settingsApi.clearSignals(signals.revision); if (!gate.isCurrent(token)) return; const value = await settingsApi.signals(); if (gate.isCurrent(token)) { if (signalEpoch() === operationEpoch) setSignalsEnabled(value.enabled); setSignals(value); } }
    catch { if (gate.isCurrent(token)) setSignalError(true); }
    finally { if (gate.isCurrent(token)) setSignalBusy(false); }
  }
  async function check() {
    if (checking) return;
    const token = gate.issue('integrity', scope); if (!token) return;
    setChecking(true); setIntegrityError(false); setIntegrityOpen(true);
    try {
      const value = await checkIntegrity();
      if (gate.isCurrent(token)) { setIntegrity(value); setCheckedAt(new Date().toISOString()); }
    } catch { if (gate.isCurrent(token)) setIntegrityError(true); }
    finally { if (gate.isCurrent(token)) setChecking(false); }
  }
  async function reloadSnapshots() {
    const token = gate.issue('snapshots', scope);
    if (!token) return;
    setLoadingSnapshots(true); setSnapshotError(false);
    try {
      const value = await loadSnapshots();
      if (gate.isCurrent(token)) {
        const rows = [...(value.snapshots || [])].sort((left, right) => compareDateValues(left.created_at, right.created_at));
        setSnapshots(rows); setSnapshotError(false);
        setRestoreOperations(value.restore_operations || []);
        const restored = (value.restore_operations || []).filter(operation => operation.state === 'adopted').at(-1);
        setSelectedSnapshot(previous => rows.some(row => row.snapshot_id === previous) ? previous
          : rows.some(row => row.snapshot_id === restored?.target_snapshot_id) ? restored.target_snapshot_id : rows.at(-1)?.snapshot_id || null);
        return rows;
      }
    } catch { if (gate.isCurrent(token)) setSnapshotError(true); }
    finally { if (gate.isCurrent(token)) setLoadingSnapshots(false); }
  }
  useEffect(() => {
    setSnapshots(null); setSnapshotError(false); setLoadingSnapshots(false); setError(''); setBusy(false);
    setSelectedSnapshot(null); setRestorePlan(null); setRestoring(false); setRestoreError(false); setRestoreStatus(''); setRestoreOperations([]); setRestoreOperationId(null); setRestoreReadError(false);
    restoreAttempt.current = null;
  }, [scope]);
  useEffect(() => {
    if (open) reloadSnapshots();
  }, [open, scope, loadSnapshots]);
  useEffect(() => {
    const operation = restoreOperations.filter(value => value.target_snapshot_id === selectedSnapshot).at(-1);
    if (!restoreOperationId && ['queued', 'adopting'].includes(operation?.state)) setRestoreOperationId(operation.operation_id);
  }, [restoreOperations, selectedSnapshot, restoreOperationId]);
  useEffect(() => {
    if (!restoreOperationId) return;
    const token = gate.issue('restore-poll', scope), identity = deviceIdentity();
    let timer, disposed = false;
    const current = () => !disposed && gate.isCurrent(token) && isDeviceIdentity(identity);
    const poll = async () => {
      try {
        const value = await loadMemoryRestore({ operationId: restoreOperationId });
        if (!current()) return;
        if (value.operation_id !== restoreOperationId || value.target_snapshot_id !== selectedSnapshot) throw new Error('restore_identity_changed');
        setRestoreReadError(false);
        if (value.status === 'restored' && value.restore_executed === true && value.runtime_ready === true) {
          const address = new URL(window.location.href), params = new URLSearchParams(address.hash.slice(1));
          if (params.get('view') === 'settings') { params.set('section', 'data'); address.hash = params.toString(); window.history.replaceState(window.history.state, '', address); }
          setRestoreStatus('adopted'); window.location.reload(); return;
        }
        if (value.status === 'failed') { setRestoreStatus(value.requires_restart ? 'restart_required' : 'failed'); setRestoreError(true); setRestoreOperationId(null); return; }
        if (value.state === 'prepared') setRestoreStatus('prepared_restart_required');
        else if (['queued', 'adopting', 'adopted'].includes(value.state)) setRestoreStatus('restore_queued');
      } catch (failure) {
        if (current()) {
          if (failure?.code === 'restore_operation_not_found') { setRestoreStatus('not_accepted'); setRestoreOperationId(null); return; }
          setRestoreReadError(true);
        }
      }
      if (current()) timer = setTimeout(poll, 1000);
    };
    poll();
    return () => { disposed = true; clearTimeout(timer); gate.invalidate('restore-poll'); };
  }, [restoreOperationId, selectedSnapshot, scope]);
  async function backup() {
    if (busy || restoring || restoreLocked || transferBusy) return;
    const token = gate.issue('backup', scope); if (!token) return;
    setBusy(true); setError('');
    try { await createSnapshot(); if (gate.isCurrent(token)) await reloadSnapshots(); }
    catch (failure) { if (gate.isCurrent(token)) setError(['backup_source_changed', 'backup_sqlite_busy', 'backup_sqlite_failed', 'backup_failed', 'server_unavailable', 'invalid_response', 'network_error'].includes(failure?.code) ? failure.code : 'backup_failed'); }
    finally { if (gate.isCurrent(token)) setBusy(false); }
  }
  function selectSnapshot(id) {
    gate.invalidate('restore'); gate.invalidate('restore-poll');
    setSelectedSnapshot(id); setRestorePlan(null); setRestoreError(false); setRestoreStatus(''); setRestoreOperationId(null); setRestoreReadError(false);
    restoreAttempt.current = null;
  }
  async function imported(id) {
    const token = gate.issue('import-selection', scope);
    const rows = await reloadSnapshots();
    if (gate.isCurrent(token) && rows?.some(row => row.snapshot_id === id && row.verified === true)) { setOpen(true); selectSnapshot(id); }
  }
  async function cancelRestorePlan() {
    if (!restorePlan || restoring || transferBusy || restoreAttempt.current || restoreOperationId || restoreStatus !== 'preview_ready') return;
    const token = gate.issue('restore', scope), plan = restorePlan;
    setRestoring(true);
    try {
      const result = await releaseRestorePreview({ previewId: plan.preview_id, identity: plan.identity });
      if (gate.isCurrent(token)) {
        if (['released', 'settled'].includes(result.status)) {
          setRestorePlan(null); setRestoreStatus(''); setRestoreError(false);
        } else if (['confirmation_pending', 'restore_accepted'].includes(result.status)) {
          restoreAttempt.current = { ...plan, identity: plan.identity, snapshotId: plan.target_snapshot_id };
          setRestoreStatus('restore_unknown'); setRestoreOperationId(plan.operation_id);
        } else setRestoreError(true);
      }
    } catch { if (gate.isCurrent(token)) setRestoreError(true); }
    finally { if (gate.isCurrent(token)) setRestoring(false); }
  }
  async function restore() {
    if (restoring || busy || transferBusy || !selectedSnapshot) return;
    const token = gate.issue('restore', scope); if (!token) return;
    const identity = deviceIdentity(), current = () => gate.isCurrent(token) && isDeviceIdentity(identity);
    const snapshotId = selectedSnapshot;
    setRestoring(true); setRestoreError(false);
    try {
      if (!restorePlan) {
        restoreAttempt.current = null;
        const plan = await createRollbackPlan({ snapshotId });
        if (!plan?.rollback_id || !plan.operation_id || !plan.preview_id || !Number.isFinite(Date.parse(plan.preview_expires_at)) || plan.target_snapshot_id !== snapshotId) throw new Error('invalid_restore_plan');
        if (current()) {
          setRestorePlan({ ...plan, identity });
          setRestoreStatus('preview_ready');
        }
      } else {
        if (!isDeviceIdentity(restorePlan.identity) || restorePlan.target_snapshot_id !== snapshotId) throw new Error('restore_identity_changed');
        restoreAttempt.current = { ...restorePlan, identity, snapshotId };
        const result = await restoreMemorySnapshot({ snapshotId, rollbackId: restorePlan.rollback_id,
          operationId: restorePlan.operation_id, useDesktop: !server });
        if (result?.target_snapshot_id !== snapshotId || !['cancelled', 'restored_relaunching', 'recovery_pending_relaunch', 'prepared_restart_required', 'restore_queued'].includes(result.status)
          || result.restore_executed !== (result.status === 'restored_relaunching')) throw new Error('invalid_restore_result');
        if (['restore_queued', 'prepared_restart_required'].includes(result.status) && result.operation_id !== restorePlan.operation_id) throw new Error('restore_operation_required');
        if (current()) { setRestorePlan(null); setRestoreStatus(result.status); if (result.status === 'cancelled') restoreAttempt.current = null; if (result.status === 'restore_queued') setRestoreOperationId(result.operation_id); }
      }
    } catch (failure) {
      if (current()) {
        if (['restore_preview_expired', 'restore_preview_changed', 'restore_preview_identity_changed', 'restore_plan_changed'].includes(failure?.code)) {
          restoreAttempt.current = null; setRestoreOperationId(null); setRestorePlan(null);
          setRestoreStatus(failure.code === 'restore_plan_changed' ? 'preview_changed' : 'preview_expired');
        } else if (failure?.code === 'restore_preview_busy') {
          setRestoreStatus('preview_busy'); setRestoreError(true);
        } else if (restoreAttempt.current?.operation_id) {
          setRestoreStatus('restore_unknown'); setRestoreOperationId(restoreAttempt.current.operation_id);
        } else { setRestoreError(true); setRestorePlan(null); }
      }
    }
    finally { if (current()) setRestoring(false); }
  }
  async function resumeRestore() {
    const operationId = restoreAttempt.current?.operation_id || previousRestore?.operation_id;
    if (!operationId || restoring || transferBusy) return;
    const token = gate.issue('restore', scope), identity = deviceIdentity();
    if (!token) return;
    const current = () => gate.isCurrent(token) && isDeviceIdentity(identity);
    setRestoring(true); setRestoreReadError(false);
    try {
      const value = await resumeMemoryRestore({ operationId });
      if (value.operation_id !== operationId || value.target_snapshot_id !== selectedSnapshot || value.status !== 'restore_queued') throw new Error('invalid_restore_result');
      if (current()) { setRestoreStatus('restore_queued'); setRestoreError(false); setRestoreOperationId(operationId); }
    } catch {
      if (current()) { setRestoreStatus('restore_unknown'); setRestoreOperationId(operationId); }
    } finally { if (current()) setRestoring(false); }
  }
  async function recheckRestore() {
    if (restoring || busy || transferBusy || !selectedSnapshot) return;
    const token = gate.issue('restore', scope), identity = deviceIdentity();
    if (!token) return;
    const current = () => gate.isCurrent(token) && isDeviceIdentity(identity);
    const previous = restorePlan;
    setRestoring(true);
    try {
      if (previous?.preview_id) {
        const released = await releaseRestorePreview({ previewId: previous.preview_id, identity: previous.identity });
        if (!['released', 'settled'].includes(released.status)) {
          if (current()) { setRestoreStatus('restore_unknown'); setRestoreOperationId(previous.operation_id); }
          return;
        }
      }
      restoreAttempt.current = null; setRestorePlan(null); setRestoreError(false); setRestoreStatus('');
      const plan = await createRollbackPlan({ snapshotId: selectedSnapshot });
      if (!plan.rollback_id || !plan.operation_id || !plan.preview_id || !Number.isFinite(Date.parse(plan.preview_expires_at)) || plan.target_snapshot_id !== selectedSnapshot) throw new Error('invalid_restore_plan');
      if (current()) { setRestorePlan({ ...plan, identity }); setRestoreStatus('preview_ready'); }
    } catch { if (current()) setRestoreError(true); }
    finally { if (current()) setRestoring(false); }
  }
  const selected = snapshots?.find(row => row.snapshot_id === selectedSnapshot);
  const previousRestore = restoreOperations.filter(operation => operation.target_snapshot_id === selectedSnapshot).at(-1);
  const displayedRestore = restoreStatus || (previousRestore?.error_code ? 'failed' : { prepared: 'prepared_restart_required', queued: 'restore_queued', adopting: 'restore_queued', adopted: previousRestore?.runtime_ready ? 'adopted' : 'adoption_pending', failed: 'failed' }[previousRestore?.state] || '');
  const restoreLocked = ['restored_relaunching', 'recovery_pending_relaunch', 'restore_queued', 'restore_unknown', 'restart_required'].includes(displayedRestore);
  return <div className="settings-data-layout">
    <h2 className="settings-support-cap">备份<span>{snapshots ? snapshots.length : '—'}</span></h2>
    <section className="settings-card settings-backup-card">
      <Row dot={<span className="settings-glyph"><Icon name="save" size={18}/></span>} title="备份" expanded={open}
        trailing={<><BackupImportButton key={scope.key} scopeKey={scope.key} blocked={busy || restoring || restoreLocked}
          onBusyChange={setTransferBusy} onImported={imported}/><button type="button" className="settings-support-button is-primary" disabled={busy || loadingSnapshots || restoring || restoreLocked || transferBusy} onClick={backup}>{busy ? '备份中' : '立即备份'}</button>{error && <span role="alert" title={error}>✕ <button type="button" aria-label="重试创建备份" disabled={busy || loadingSnapshots || restoring || restoreLocked || transferBusy} onClick={backup}>重试</button></span>}{snapshotError && <span role="alert" title="backup_read_failed">✕ <button type="button" aria-label="重试读取备份" disabled={busy || loadingSnapshots || restoring || restoreLocked} onClick={reloadSnapshots}>重试</button></span>}</>}
        onOpen={() => setOpen(value => !value)}/>
      {open && <>
        <div className="settings-backup-timeline" role="group" aria-label="备份时间线">
          {loadingSnapshots && !snapshots && <span role="status">读取中</span>}
          {snapshots?.length === 0 && <span className="settings-support-empty">暂无备份</span>}
          {(snapshots || []).map(row => <button type="button" key={row.snapshot_id}
            className={`settings-backup-point${selectedSnapshot === row.snapshot_id ? ' is-selected' : ''}${row.verified === false ? ' is-failed' : ''}`}
            aria-pressed={selectedSnapshot === row.snapshot_id} aria-label={row.created_at || row.snapshot_id}
            title={row.created_at || row.snapshot_id} disabled={restoring || busy || restoreLocked || transferBusy} onClick={() => selectSnapshot(row.snapshot_id)}>
            <span aria-hidden="true"/><time>{formatLocalShortDate(row.created_at)}</time>
          </button>)}
        </div>
        {selected && <div className="settings-backup-detail">
          <div><span>时间</span><time>{formatLocalShortDate(selected.created_at)}</time></div>
          <div><span>大小</span><strong>{formatBytes(selected.size_bytes)}</strong></div>
          <div><span>校验</span><strong className={selected.verified === false ? 'is-failed' : ''} title={selected.verified === false ? 'backup_verification_failed' : undefined}>{selected.verified === true ? '✓' : selected.verified === false ? '✕' : '—'}</strong></div>
          {selected.restorable === true && <BackupDownloadButton key={`${scope.key}:${selected.snapshot_id}`} scopeKey={scope.key}
            snapshotId={selected.snapshot_id} blocked={busy || restoring || restoreLocked || transferBusy || selected.verified !== true}/>}
          {selected.restorable === true && <button type="button" className={`settings-support-button${restorePlan ? ' is-danger' : ''}`}
            disabled={busy || restoring || transferBusy || selected.verified !== true || restoreLocked || displayedRestore === 'failed'} title="校验并恢复所选备份，当前资料保留为恢复前副本" onClick={displayedRestore === 'prepared_restart_required' && (restoreAttempt.current || previousRestore?.operation_id) ? resumeRestore : restore}>{restoring || displayedRestore === 'restore_queued' ? '恢复中' : restoreLocked ? '待完成' : displayedRestore === 'not_accepted' ? '重试本次恢复' : restorePlan ? '确认恢复' : displayedRestore === 'prepared_restart_required' ? '继续本次恢复' : '恢复到此'}</button>}
          {restorePlan && displayedRestore === 'preview_ready' && !restoreAttempt.current && !restoreOperationId && <div role="group" aria-label="确认恢复备份">
            <span>恢复至 <time dateTime={selected.created_at || undefined}>{formatLocalShortDate(selected.created_at)}</time></span>
            <span>当前资料保留为恢复前副本</span>
            <span>确认有效至 <time dateTime={restorePlan.preview_expires_at}>{new Date(restorePlan.preview_expires_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</time></span>
            <button type="button" className="settings-support-button" disabled={restoring || transferBusy} onClick={cancelRestorePlan}>取消</button>
          </div>}
          {(restoreError || displayedRestore === 'failed') && displayedRestore !== 'restart_required' && <span role="alert">恢复未完成</span>}
          {['failed', 'not_accepted', 'preview_expired', 'preview_changed'].includes(displayedRestore) && <button type="button" className="settings-support-button" disabled={restoring || transferBusy} onClick={recheckRestore}>重新核对所选备份</button>}
          {displayedRestore === 'preview_expired' && <span role="alert">恢复确认已到期 · 请重新核对</span>}
          {displayedRestore === 'preview_changed' && <span role="alert">当前资料已变化 · 请重新核对</span>}
          {displayedRestore === 'preview_busy' && <span role="alert">维护正在收尾或已有恢复预览 · 请稍后重试</span>}
          {displayedRestore === 'restart_required' && <><span role="alert">恢复未完成 · 需要重启核对</span><button type="button" className="settings-support-button" disabled={restoring || transferBusy} onClick={resumeRestore}>重启核对本次恢复</button></>}
          {displayedRestore === 'prepared_restart_required' && <span role="status">已准备 · 待运行宿主采用</span>}
          {displayedRestore === 'restored_relaunching' && <span role="status">已恢复 · 重启中</span>}
          {displayedRestore === 'recovery_pending_relaunch' && <span role="status">切换待核对 · 重启中</span>}
          {displayedRestore === 'cancelled' && <span role="status">已取消</span>}
          {displayedRestore === 'adopted' && <span role="status">已恢复</span>}
          {displayedRestore === 'adoption_pending' && <span role="status">已采用 · 等待应用就绪</span>}
          {displayedRestore === 'restore_queued' && <span role="status">正在恢复</span>}
          {displayedRestore === 'restore_unknown' && <span role="status">正在核对此次恢复结果</span>}
          {displayedRestore === 'not_accepted' && <span role="status">此次恢复尚未受理</span>}
          {restoreReadError && <span role="alert">恢复进度暂不可读</span>}
        </div>}
      </>}
      <BackupScheduleSettings key={scope.key} scopeKey={scope.key} refreshEpoch={scheduleRefresh} blocked={restoring || restoreLocked}/>
    </section>
    <h2 className="settings-support-cap">检查</h2>
    <section className="settings-card">
      <Row dot={<span className="settings-glyph"><Icon name="check" size={18}/></span>} title="资料完整性" sub={checkedAt ? formatLocalShortDate(checkedAt) : undefined}
        expanded={integrityOpen} onOpen={() => integrity ? setIntegrityOpen(value => !value) : check()}
        meta={checking ? '…' : integrity ? integrity.ok ? '✓' : `✕ ${integrity.problems.reduce((count, problem) => count + problem.count, 0)}` : '—'}
        trailing={<><button type="button" className="settings-support-button" disabled={checking} onClick={check}>检查</button>{integrityError && <span role="alert">检查未完成</span>}</>}/>
      {integrityOpen && integrity && <div className="settings-expand">{integrity.problems.map(problem =>
        <Row key={problem.code} title={<span title={problem.code}>✕</span>} meta={String(problem.count)} readOnly className="settings-integrity-reason"/>)}</div>}
    </section>
    <h2 className="settings-support-cap">日常</h2>
    <section className="settings-card">
      <DailyInspirationSettings key={projectId} projectId={projectId}/>
      <Row readOnly dot={<span className="settings-glyph"><Icon name="book" size={18}/></span>} title="使用记录" className="settings-signals-row" meta={<span className="settings-signals-controls" title={`保留 180 天${server ? ' · 管理员可见' : ''}`}><span>{signals ? `${signals.count} 条` : '— 条'}</span><button type="button" className={armed ? 'settings-signals-clear is-confirming' : 'settings-signals-clear'} aria-label={signalError ? '重试读取使用记录' : armed ? '确认清除使用记录' : '清除使用记录'} title={signalError ? 'signal_settings_failed' : undefined} disabled={signalBusy || !signals && !signalError} onClick={signalError ? reloadSignals : clearSignals}>{signalError ? '重试' : armed ? '确认' : '清除'}</button><Switch label="使用记录" checked={signals?.enabled ?? false} disabled={!signals || signalBusy} onChange={changeSignals}/></span>}/>
    </section>
  </div>;
}
