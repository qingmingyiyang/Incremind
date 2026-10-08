export function browserStorage() {
  try { return globalThis.localStorage || null; } catch { return null; }
}

export function readStoredJson(key, storage = browserStorage()) {
  if (!storage) return { value: null, unavailable: true, invalid: false };
  let raw;
  try { raw = storage.getItem(key); } catch { return { value: null, unavailable: true, invalid: false }; }
  if (raw === null) return { value: null, unavailable: false, invalid: false };
  try { return { value: JSON.parse(raw), unavailable: false, invalid: false }; }
  catch { return { value: null, unavailable: false, invalid: true }; }
}

export function writeStoredJson(key, value, storage = browserStorage()) {
  try { if (!storage) return false; storage.setItem(key, JSON.stringify(value)); return true; }
  catch { return false; }
}

export function removeStoredItem(key, storage = browserStorage()) {
  try { if (!storage) return false; storage.removeItem(key); return true; }
  catch { return false; }
}
