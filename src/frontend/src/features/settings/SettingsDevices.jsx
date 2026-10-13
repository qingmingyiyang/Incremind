import { useEffect, useState } from 'react';
import { Row } from '../../shared/ui';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { formatLocalShortDate } from '../../shared/lib/time';
import { forgetDeviceCredential, readUserSpace, userStorageKey } from '../../shared/api/deviceTransport';
import { devicesApi } from './devicesApi';
import { SettingsUsers } from './SettingsUsers';
import { DevicePairing } from './DevicePairing';
import { DeleteConfirmation } from './DeleteConfirmation';

export function SettingsDevices({ projectId }) {
  const [value, setValue] = useState(null), [qr, setQr] = useState(null);
  const [error, setError] = useState(''), [busy, setBusy] = useState(false), [refresh, setRefresh] = useState(0);
  const gate = useRequestScope(userStorageKey(projectId)), scope = gate.scope;
  useEffect(() => {
    const token = gate.issue('load', scope);
    setValue(null); setQr(null); setBusy(false); setError('');
    devicesApi.list().then(result => { if (gate.isCurrent(token)) setValue(result); })
      .catch(() => { if (gate.isCurrent(token)) setError('读取未完成 · 重试'); });
    return () => { gate.invalidate('load'); gate.invalidate('write'); };
  }, [scope, refresh]);
  async function run(operation) {
    if (busy) return;
    const token = gate.issue('write', scope); if (!token) return;
    setBusy(true); setError('');
    try { await operation(token); }
    catch (reason) { if (gate.isCurrent(token)) setError(reason.code === 'device_delete_requires_revoked' ? '设备仍有效 · 先作废' : reason.status === 409 ? '设备已变化 · 刷新' : '操作未完成 · 重试'); }
    finally { if (gate.isCurrent(token)) setBusy(false); }
  }
  return <section>{value?.mode === 'server' && <SettingsUsers projectId={projectId}/>}
    <section className="settings-current-devices" aria-label="当前空间设备"><h2>{readUserSpace() ? `${readUserSpace().name}的设备` : '当前空间设备'}</h2>
    {error && <p role="alert">{error} <button type="button" onClick={() => setRefresh(value => value + 1)}>刷新</button></p>}
    {!value ? <span role="status">读取中</span> : value.items.map(device => <div key={device.device_id}><Row className="settings-device-row" readOnly title={device.name}
      meta={device.revoked_at ? '已作废' : device.device_id === 'desktop' ? '本机' : formatLocalShortDate(device.last_seen_at)}
      trailing={!device.revoked_at && device.device_id !== 'desktop' && <button type="button" className="settings-device-revoke" disabled={busy} aria-label={`作废${device.name}`} onClick={() => run(async token => {
        const result = await devicesApi.revoke(device);
        if (!gate.isCurrent(token)) return;
        setValue(old => ({ ...old, items: old.items.map(row => row.device_id === device.device_id ? result : row) }));
        if (value.device_id === device.device_id) forgetDeviceCredential();
      })}>作废</button>}/>
      {device.revoked_at && device.device_id !== 'desktop' && <div className="settings-expand"><DeleteConfirmation key={`${device.device_id}:${device.revision}`} name={device.name}
        label={`删除${device.name}的设备记录`} consequence={`将永久删除 ${device.name} 的设备记录和配对记录，保留空间资料和访问记录。`} disabled={busy}
        onConfirm={() => run(async token => {
          const result = await devicesApi.remove(device);
          if (result?.deleted !== true || result.device_id !== device.device_id) throw new Error('device_delete_unconfirmed');
          if (gate.isCurrent(token)) setValue(old => ({ ...old, items: old.items.filter(row => row.device_id !== device.device_id) }));
        })}/></div>}
    </div>)}
    <div className="settings-device-pair"><button type="button" disabled={busy || !value} onClick={() => run(async token => {
      setQr(null); const result = await devicesApi.pair();
      if (gate.isCurrent(token)) setQr(result);
    })}>{qr ? '重新生成' : '添加设备'}</button>
      <DevicePairing value={qr}/>
    </div>
    </section>
  </section>;
}
