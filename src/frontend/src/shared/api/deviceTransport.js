import { readStoredJson, writeStoredJson, removeStoredItem } from '../lib/browserStorage';

const SLOT = 'chriptmas-server-device';
let inMemory = null;
let sessionInitialized = false;
let selectedUser = null;
let spaceRevision = 0;
let mobileMode = false;
let mobileSession = null;
const credentialPattern = /^[A-Za-z0-9_-]{43}$/;
const identityPattern = /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/;
const origin = () => globalThis.location?.origin;

export function isAndroidWorkbench() {
  return globalThis.navigator?.userAgent?.split(/\s+/).includes('IncremindAndroid/1') === true;
}

export function readDeviceCredential() {
  if (globalThis.electronAPI?.backendBaseUrl) return null;
  if (mobileMode || isAndroidWorkbench()) return mobileSession;
  if (!sessionInitialized) {
    inMemory = readStoredJson(SLOT).value;
    sessionInitialized = true;
  }
  const value = inMemory;
  return value?.origin === origin() && credentialPattern.test(value.key || '') ? value : null;
}

export function saveDeviceCredential(value) {
  if (mobileMode || isAndroidWorkbench() || !credentialPattern.test(value?.key || '') || !value?.device?.device_id) throw new Error('device_response_invalid');
  inMemory = { origin: origin(), key: value.key, device: value.device };
  sessionInitialized = true;
  selectedUser = null; spaceRevision += 1;
  writeStoredJson(SLOT, inMemory);
}

export function saveMobileDeviceSession(device) {
  if (typeof device?.user_id !== 'string' || typeof device?.device_id !== 'string'
      || !identityPattern.test(device.user_id) || !identityPattern.test(device.device_id)) throw new Error('device_response_invalid');
  if (mobileSession?.device.device_id === device.device_id && mobileSession.device.user_id === device.user_id) return;
  mobileMode = true;
  mobileSession = { origin: origin(), mobile: true, device: { user_id: device.user_id, device_id: device.device_id } };
  selectedUser = null;
  spaceRevision += 1;
}

function identityKey(credential) {
  // 移动证书由系统完成认证，此处只用公开编号区分请求作用域。
  return credential?.mobile ? `mobile:${credential.device.user_id}:${credential.device.device_id}` : credential?.key || null;
}

export function deviceIdentity() { return { key: identityKey(readDeviceCredential()), revision: spaceRevision }; }

export function isDeviceIdentity(identity) {
  return identity?.revision === spaceRevision && identity?.key === identityKey(readDeviceCredential());
}

export function forgetDeviceCredential(identity) {
  if (identity && !isDeviceIdentity(identity)) return false;
  mobileSession = null;
  inMemory = null;
  sessionInitialized = true;
  selectedUser = null; spaceRevision += 1;
  removeStoredItem(SLOT);
  globalThis.window?.dispatchEvent(new Event('chriptmas-device-required'));
  return true;
}

export function readUserSpace() { return readDeviceCredential() ? selectedUser : null; }

export function selectUserSpace(user) {
  const credential = readDeviceCredential();
  if (!credential || (user && (!identityPattern.test(user.user_id || '') || credential.mobile && user.user_id !== credential.device.user_id))) throw new Error('user_space_invalid');
  selectedUser = user?.user_id === credential.device.user_id ? null : user;
  spaceRevision += 1;
  globalThis.window?.dispatchEvent(new CustomEvent('chriptmas-user-space', { detail: selectedUser }));
}

export function userStorageKey(key) {
  const credential = readDeviceCredential();
  if (!credential) return key;
  if (credential.mobile) return `chriptmas-mobile:${credential.device.user_id}:${credential.device.device_id}:${key}`;
  return `chriptmas-user:${credential.device.user_id}:${selectedUser?.user_id || credential.device.user_id}:${key}`;
}

export function isProductUrl(input) {
  try {
    const url = new URL(typeof input === 'string' || input instanceof URL ? String(input) : input.url, globalThis.location?.href);
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password && url.origin === origin()
      && (url.pathname.startsWith('/api/') || ['/local-model/v1/models', '/local-model/v1/chat/completions'].includes(url.pathname));
  } catch { return false; }
}

export async function productFetch(input, init = {}) {
  const url = isProductUrl(input) ? new URL(typeof input === 'string' || input instanceof URL ? String(input) : input.url, globalThis.location?.href) : null;
  const publicPath = url && ['/api/health', '/api/v2/devices/exchange', '/api/v2/auth/login'].includes(url.pathname);
  const credential = url && !publicPath ? readDeviceCredential() : null;
  const revision = spaceRevision, target = credential ? readUserSpace() : null, key = identityKey(credential);
  // 登录密码和配对码均禁止通过重定向发送。
  let options = publicPath && url.pathname !== '/api/health' ? { ...init, redirect: 'error' } : init;
  if (credential) {
    const headers = new Headers(init.headers || (typeof input === 'object' ? input.headers : undefined));
    if (credential.mobile) {
      headers.delete('Authorization');
      headers.delete('X-Chriptmas-Target-User');
    } else {
      headers.set('Authorization', 'Bearer ' + credential.key);
      if (target) headers.set('X-Chriptmas-Target-User', target.user_id);
    }
    options = { ...init, headers, redirect: 'error' };
  }
  const response = await globalThis.fetch(input, options);
  if (!credential) return response;
  const currentKey = identityKey(readDeviceCredential());
  if (revision !== spaceRevision && (credential.mobile || key === currentKey)) throw new Error('user_space_changed');
  if (response.status === 401 && key === currentKey) forgetDeviceCredential();
  return response;
}

export async function downloadProductFile(url, name, isCurrent = () => true, { signal } = {}) {
  const revision = spaceRevision;
  if (!isProductUrl(url)) throw new Error('device_download_destination_invalid');
  const response = await productFetch(url, { cache: 'no-store', signal });
  return downloadResponseBlob(response, name, () => revision === spaceRevision && isCurrent(), { signal });
}

export async function downloadResponseBlob(response, name, isCurrent = () => true, { signal } = {}) {
  const revision = spaceRevision;
  if (!response.ok) {
    await response.body?.cancel();
    throw new Error('device_download_failed');
  }
  if (signal?.aborted || !isCurrent()) { await response.body?.cancel(); return; }
  const blob = await response.blob();
  if (signal?.aborted || !isCurrent() || revision !== spaceRevision) return;
  const encoding = response.headers.get('content-encoding');
  const length = response.headers.get('content-length');
  if ((!encoding || encoding.toLowerCase() === 'identity') && length !== null
    && (!/^\d+$/.test(length) || Number(length) !== blob.size)) throw new Error('device_download_size_mismatch');
  const link = document.createElement('a'), href = URL.createObjectURL(blob);
  try {
    link.href = href; link.download = name || '原件'; link.rel = 'noreferrer';
    document.body.appendChild(link); link.click();
    return { size_bytes: blob.size, filename: link.download };
  } finally {
    link.remove(); setTimeout(() => URL.revokeObjectURL(href), 1000);
  }
}
