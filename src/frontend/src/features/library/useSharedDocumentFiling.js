import { useEffect, useRef, useState } from 'react';
import { deviceIdentity, isDeviceIdentity, readDeviceCredential, userStorageKey } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { clearSharedCommand, readSharedCommand, saveSharedCommand } from '../shared-projects/sharedProjectCommands';
import { sharedProjectsApi } from '../shared-projects/sharedProjectsApi';

const part = encodeURIComponent;
const segment = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/.test(value);

export function useSharedDocumentFiling({ enabled, project, row, onChanged, onError }) {
  const actor = deviceIdentity(), device = readDeviceCredential()?.device?.device_id;
  const key = enabled && device ? userStorageKey(`shared-document-filing:${[device, project, row.document_id].map(part).join(':')}`) : null;
  const gate = useRequestScope(`${key}:${actor.revision}:${row.revision}`);
  const scope = gate.scope, active = useRef(null);
  const [status, setStatus] = useState(null);
  const current = status?.scope === scope ? status : null;
  function update(change) {
    setStatus(previous => ({ ...(previous?.scope === scope ? previous : {}), scope, ...change }));
  }
  function retained() {
    const command = readSharedCommand(key);
    if (command && (!segment(command.id) || !segment(command.projectId)
        || command.expected?.action !== 'file-document' || command.expected.document !== row.document_id
        || command.body?.source_project_id !== project || !Number.isSafeInteger(command.body.expected_revision)
        || command.body.expected_revision < 1 || typeof command.body.target_namespace !== 'string'
        || command.path !== `/${part(command.projectId)}/filings/${part(row.document_id)}`)) {
      throw new Error('移入记录未核对');
    }
    return command;
  }
  useEffect(() => {
    if (!key) return;
    try {
      const command = retained();
      update({ command, phase: 'unknown' });
      if (command) void check();
    }
    catch (error) { onError?.(error.message); update({ blocked: true }); }
    return () => { active.current?.abort(); active.current = null; };
  }, [scope]);
  async function run(operation) {
    if (!key || active.current || !isDeviceIdentity(actor)) return;
    const token = gate.issue('filing', scope);
    if (!token) return;
    const controller = new AbortController();
    active.current = controller;
    const live = () => gate.isCurrent(token) && isDeviceIdentity(actor);
    update({ busy: true, error: null }); onError?.('');
    try { await operation(controller.signal, live); }
    catch (error) {
      if (live()) { update({ error: error.message }); onError?.(error.message); }
    }
    finally {
      if (active.current === controller) active.current = null;
      if (live()) update({ busy: false });
    }
  }
  function receive(command, result) {
    if (!result || result.command_id !== command.id || result.target_project_id !== command.projectId
        || result.source_document_id !== row.document_id || result.source_project_id !== project
        || result.target_namespace !== command.body.target_namespace
        || !(result.state === 'pending' && result.target_document_id === null
          || result.state === 'filed' && segment(result.target_document_id))) {
      throw new Error('移入结果未核对');
    }
    if (result.state === 'pending') { update({ command, phase: 'pending' }); return; }
    clearSharedCommand(command, { purgeRecord: true });
    update({ command: null, phase: null });
    onChanged?.(result);
  }
  async function submit(command, signal, live) {
    update({ command, phase: 'unknown' });
    const result = await sharedProjectsApi.fileDocument(command.projectId, row.document_id, command.body, command.id, signal);
    if (live()) receive(command, result);
  }
  const check = () => run(async (signal, live) => {
    const command = retained();
    if (!command) { update({ command: null, phase: null }); return; }
    update({ command, phase: 'unknown' });
    try {
      const result = await sharedProjectsApi.filingCommand(command.projectId, row.document_id, command.id, signal);
      if (live()) receive(command, result);
    } catch (error) {
      if (error.status !== 404 || error.code !== 'shared_filing_not_received') throw error;
      if (live()) update({ command, phase: 'not_received' });
    }
  });
  return {
    command: current?.command, busy: current?.busy, blocked: current?.blocked, error: current?.error,
    canResume: ['pending', 'not_received'].includes(current?.phase) && current?.command?.body.expected_revision === row.revision,
    obsolete: current?.phase === 'not_received' && current?.command?.body.expected_revision !== row.revision,
    check,
    discard: () => run(async () => {
      if (current?.phase !== 'not_received') return;
      const command = retained();
      if (command?.id !== current.command?.id) throw new Error('移入记录已变化 · 核对');
      clearSharedCommand(command, { purgeRecord: true });
      update({ command: null, phase: null });
    }),
    resume: () => run(async (signal, live) => {
      const command = retained();
      if (command?.id !== current?.command?.id || command?.body.expected_revision !== row.revision
          || !['pending', 'not_received'].includes(current?.phase)) throw new Error('移入记录已变化 · 核对');
      await submit(command, signal, live);
    }),
    start: (target, scene) => run(async (signal, live) => {
      const pending = retained();
      if (pending) { update({ command: pending, phase: 'unknown' }); return; }
      const command = saveSharedCommand({
        version: 1, storageKey: key, id: crypto.randomUUID(), path: `/${part(target.project_id)}/filings/${part(row.document_id)}`,
        projectId: target.project_id, expected: { action: 'file-document', document: row.document_id },
        body: { source_project_id: project, expected_revision: row.revision, scene: scene || null, target_namespace: target.namespace },
        createdAt: new Date().toISOString(), phase: 'prepared',
      });
      await submit(command, signal, live);
    }),
  };
}
