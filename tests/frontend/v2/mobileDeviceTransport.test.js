import { expect, it } from 'vitest';
import {
  deviceIdentity, forgetDeviceCredential, isDeviceIdentity, readDeviceCredential,
  saveDeviceCredential, saveMobileDeviceSession, selectUserSpace, userStorageKey,
} from '../../../src/frontend/src/shared/api/deviceTransport';

it('keeps certificate identity in memory and isolates the same account on two devices', () => {
  localStorage.clear();
  expect(() => saveMobileDeviceSession({ user_id: 'alice', device_id: '../invalid' })).toThrow('device_response_invalid');
  saveMobileDeviceSession({ user_id: 'alice', device_id: 'android-a' });
  const first = deviceIdentity();
  const firstDraft = userStorageKey('draft');
  expect(readDeviceCredential()).toEqual({
    origin: location.origin, mobile: true, device: { user_id: 'alice', device_id: 'android-a' },
  });
  expect(localStorage.length).toBe(0);
  expect(() => selectUserSpace({ user_id: 'bob' })).toThrow('user_space_invalid');
  saveMobileDeviceSession({ user_id: 'alice', device_id: 'android-a' });
  expect(isDeviceIdentity(first)).toBe(true);

  saveMobileDeviceSession({ user_id: 'alice', device_id: 'android-b' });
  expect(userStorageKey('draft')).not.toBe(firstDraft);
  expect(forgetDeviceCredential(first)).toBe(false);
  const current = deviceIdentity();
  expect(forgetDeviceCredential(current)).toBe(true);
  expect(readDeviceCredential()).toBeNull();
  expect(isDeviceIdentity(current)).toBe(false);
  expect(() => saveDeviceCredential({
    key: 'a'.repeat(43), device: { user_id: 'alice', device_id: 'browser-a' },
  })).toThrow('device_response_invalid');
});
