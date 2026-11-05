import { useEffect, useState } from 'react';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { deviceIdentity, isDeviceIdentity, userStorageKey } from '../../shared/api/deviceTransport';
import { devicesApi } from '../settings/devicesApi';
import { SharedProjectsPanel } from './SharedProjectsPanel';
import { SharedExtraction } from './SharedExtraction';
import { SharedBackupPanel } from './SharedBackupPanel';

export function SharedProjectManagement() {
  const [deployment, setDeployment] = useState(null), [error, setError] = useState(false);
  const [refresh, setRefresh] = useState(0), [epoch, setEpoch] = useState(0);
  const [extractionRefresh, setExtractionRefresh] = useState(0);
  const [backupRequest, setBackupRequest] = useState(null);
  const gate = useRequestScope(`${userStorageKey('shared-project-management')}\0${epoch}`), scope = gate.scope;
  const actor = deviceIdentity();
  const server = deployment?.scope === scope && deployment.server === true;
  useEffect(() => {
    const changed = () => { gate.invalidate('deployment'); setDeployment(null); setError(false); setBackupRequest(null); setEpoch(value => value + 1); };
    window.addEventListener('chriptmas-user-space', changed);
    window.addEventListener('chriptmas-device-required', changed);
    return () => {
      window.removeEventListener('chriptmas-user-space', changed);
      window.removeEventListener('chriptmas-device-required', changed);
    };
  }, [gate]);
  useEffect(() => {
    const token = gate.issue('deployment', scope), identity = deviceIdentity();
    setDeployment(null); setError(false);
    devicesApi.list().then(value => {
      if (!['server', 'desktop'].includes(value?.mode)) throw new Error('deployment_invalid');
      if (gate.isCurrent(token) && isDeviceIdentity(identity)) setDeployment({ scope, server: value.mode === 'server' });
    }).catch(() => { if (gate.isCurrent(token) && isDeviceIdentity(identity)) setError(true); });
    return () => gate.invalidate('deployment');
  }, [scope, refresh]);
  return <>
    {error && <p role="alert">共享项目资格未读取 · <button type="button" onClick={() => setRefresh(value => value + 1)}>重试</button></p>}
    <SharedProjectsPanel enabled={server} onProjectsChanged={() => setExtractionRefresh(value => value + 1)}
      onBackup={project => {
        if (gate.isScopeCurrent(scope) && isDeviceIdentity(actor)) setBackupRequest({ project,
          scope: userStorageKey('shared-backups-v1'), nonce: crypto.randomUUID() });
      }}/>
    <SharedBackupPanel enabled={server} request={backupRequest} refreshToken={extractionRefresh}/>
    <SharedExtraction enabled={server} refreshToken={extractionRefresh}/>
  </>;
}
