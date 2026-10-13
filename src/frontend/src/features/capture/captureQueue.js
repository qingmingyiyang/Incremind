import { productFetch, readDeviceCredential, userStorageKey } from '../../shared/api/deviceTransport';

export const captureStore = globalThis.chriptmasCaptureStore;
export function captureOwner() {
  const credential = readDeviceCredential();
  return credential ? userStorageKey('capture') + ':' + credential.device.device_id : null;
}
export async function syncCaptureOwner() {
  await captureStore.setOwner(captureOwner());
}
const pending = new Map();
export function sendCapture(id) {
  if (pending.has(id)) return pending.get(id);
  const send = async () => {
    const owner = captureOwner();
    let capture = await captureStore.get(id);
    if (!capture) throw new Error('capture_missing');
    if (capture.owner && capture.owner !== owner) throw new Error('capture_owner_changed');
    if (capture.state === 'blocked') throw new Error(capture.error);
    if (capture.state === 'done') return capture;
    capture = { ...capture, owner };
    await captureStore.put(capture);
    const form = new FormData();
    form.append('project_id', 'default'); form.append('text', capture.text);
    capture.files.forEach(file => form.append('files', file, file.name));
    if (captureOwner() !== owner) throw new Error('capture_owner_changed');
    const response = await productFetch('/api/v2/workbench/share', {
      method: 'POST', body: form, cache: 'no-store', headers: { 'Idempotency-Key': id },
    });
    if (!response.ok) {
      const error = new Error((await response.json()).detail || 'capture_failed');
      if (['share_turn_interrupted', 'share_upload_interrupted', 'idempotency_key_conflict', 'share_credentials_rejected', 'unsupported_share_file'].includes(error.message)) {
        await captureStore.put({ ...capture, state: 'blocked', error: error.message });
      }
      error.status = response.status; throw error;
    }
    const result = await response.json();
    if (captureOwner() !== owner) throw new Error('capture_owner_changed');
    if (!Array.isArray(result.results) || !result.results.length || !result.results.every(value => value.turn?.id && value.thread_id)) throw new Error('capture_failed');
    capture = { id, owner, createdAt: capture.createdAt, state: 'done', results: result.results };
    await captureStore.put(capture);
    return capture;
  };
  const promise = (globalThis.navigator?.locks ? navigator.locks.request('chriptmas-share-' + id, send) : send())
    .finally(() => pending.delete(id));
  pending.set(id, promise); return promise;
}
