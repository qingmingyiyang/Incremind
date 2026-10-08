import { productFetch } from './api/deviceTransport';
import { libraryBackendUrl } from '../features/rebuild/libraryOverviewTransport';

let epoch = 0;
let enabled = true;
const recent = new Map(), inFlight = new Set();
export const signalEpoch = () => epoch;
export function invalidateSignals(nextEnabled = enabled) {
  epoch += 1; enabled = nextEnabled; recent.clear();
  for (const controller of inFlight) controller.abort();
  inFlight.clear();
}
export function setSignalsEnabled(value) {
  if (typeof value === 'boolean' && value !== enabled) invalidateSignals(value);
}
export function sendSignal(event, capturedEpoch = epoch) {
  if (!enabled || capturedEpoch !== epoch) return;
  const now = Date.now(), object = event.object;
  const key = JSON.stringify([event.kind, event.project_id, event.turn_id || null, object?.id || null]);
  if ((recent.get(key) || 0) > now) return;
  const expires = event.kind === 'copy' ? now + 600000 : Date.UTC(new Date(now).getUTCFullYear(), new Date(now).getUTCMonth(), new Date(now).getUTCDate() + 1);
  recent.delete(key); recent.set(key, expires);
  while (recent.size > 128) recent.delete(recent.keys().next().value);
  const controller = new AbortController(); inFlight.add(controller);
  try {
    const body = { project_id: event.project_id, kind: event.kind,
      ...(event.turn_id ? { turn_id: event.turn_id } : {}), ...(object ? { object: { kind: object.kind, id: object.id, revision: object.revision } } : {}),
      client_id: globalThis.crypto?.randomUUID?.() || `signal-${now}-${Math.random().toString(36).slice(2)}` };
    Promise.resolve(productFetch(libraryBackendUrl('/api/v2/signals'), { method: 'POST', cache: 'no-store', signal: controller.signal,
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).catch(() => {}).finally(() => inFlight.delete(controller));
  } catch { inFlight.delete(controller); }
}
