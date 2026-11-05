import { expect, it } from 'vitest';
import { LOGIN_NAME_PATTERN, validLogin } from '../../../src/frontend/src/features/settings/loginRules';

it('uses the same valid Unicode sets pattern for native inputs and account validation', () => {
  const native = new RegExp(`^(?:${LOGIN_NAME_PATTERN})$`, 'v');
  for (const name of ['abc', 'A.b_c-1', 'a'.repeat(64), 'ab', 'a'.repeat(65), ' a1', 'a1 ', '中文账号', 'a+b', 'a/b']) {
    expect(validLogin(name, 'p'.repeat(15)), name).toBe(native.test(name));
  }
  expect(native.test('A.b_c-1')).toBe(true);
  expect(native.test('a/b')).toBe(false);
});

it('counts password Unicode code points and preserves significant whitespace', () => {
  expect(validLogin('abc', '😀'.repeat(14))).toBe(false);
  expect(validLogin('abc', '😀'.repeat(15))).toBe(true);
  expect(validLogin('abc', '😀'.repeat(256))).toBe(true);
  expect(validLogin('abc', '😀'.repeat(257))).toBe(false);
  expect(validLogin('abc', ' '.repeat(15))).toBe(true);
});
