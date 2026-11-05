export const LOGIN_NAME_PATTERN = '[A-Za-z0-9._\\x2d]{3,64}';
const loginName = new RegExp(`^(?:${LOGIN_NAME_PATTERN})$`, 'u');

export function validLogin(name, password) {
  const length = Array.from(password).length;
  return loginName.test(name) && length >= 15 && length <= 256;
}
