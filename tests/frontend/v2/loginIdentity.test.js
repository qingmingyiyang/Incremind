import { beforeEach, expect, it } from 'vitest';
import { deviceIdentity, forgetDeviceCredential, isDeviceIdentity, readDeviceCredential, readUserSpace, saveDeviceCredential, selectUserSpace, userStorageKey } from '../../../src/frontend/src/shared/api/deviceTransport';

const credential = (letter, user) => ({ key: letter.repeat(43), device: { device_id: `device-${user}`, user_id: user } });
beforeEach(() => { forgetDeviceCredential(); localStorage.clear(); });

it('persists login credentials and isolates drafts from another account and managed space', () => {
  saveDeviceCredential(credential('a', 'alice'));
  expect(JSON.parse(localStorage.getItem('chriptmas-server-device')).device.user_id).toBe('alice');
  const own = userStorageKey('draft');
  selectUserSpace({ user_id: 'bob', name: 'Bob' });
  const managed = userStorageKey('draft');
  expect(managed).not.toBe(own);
  saveDeviceCredential(credential('b', 'bob'));
  expect(readUserSpace()).toBeNull();
  expect(userStorageKey('draft')).not.toBe(own);
  expect(userStorageKey('draft')).not.toBe(managed);
});

it('a late logout cannot erase a newer login even with the same user', () => {
  saveDeviceCredential(credential('a', 'alice'));
  const old = deviceIdentity();
  saveDeviceCredential(credential('b', 'alice'));
  expect(isDeviceIdentity(old)).toBe(false);
  expect(forgetDeviceCredential(old)).toBe(false);
  expect(readDeviceCredential().key).toBe('b'.repeat(43));
});

it('a space switch invalidates logout identity and current logout clears selected space and storage', () => {
  saveDeviceCredential(credential('a', 'alice'));
  const own = deviceIdentity();
  selectUserSpace({ user_id: 'bob', name: 'Bob' });
  expect(forgetDeviceCredential(own)).toBe(false);
  const current = deviceIdentity();
  expect(forgetDeviceCredential(current)).toBe(true);
  expect(readDeviceCredential()).toBeNull();
  expect(readUserSpace()).toBeNull();
  expect(localStorage.getItem('chriptmas-server-device')).toBeNull();
  expect(isDeviceIdentity(current)).toBe(false);
});
