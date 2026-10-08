// Response parsing never exposes server HTML or JSON parser diagnostics to the UI.
export function responseFailure(code, message, status) {
  const error = new Error(message); error.code = code; error.status = status; return error;
}
export function assertServerAvailable(response) {
  if (response.status >= 500) throw responseFailure('server_unavailable', '连接未完成 · 重试', response.status);
}
export async function readResponseJson(response) {
  assertServerAvailable(response);
  try { return await response.json(); }
  catch { throw responseFailure('invalid_response', '读取未完成 · 重试', response.status); }
}
