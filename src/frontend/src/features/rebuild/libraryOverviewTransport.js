/**
 * Shared transport primitives for the library overview facade.
 *
 * Endpoint ownership and response interpretation remain in
 * `libraryOverviewApi.js`; this module only resolves the active desktop
 * backend and provides the established JSON fallback behavior.
 */
export function libraryBackendUrl(endpoint) {
  const electronBackend = globalThis.electronAPI?.backendBaseUrl;
  if (electronBackend) {
    return `${electronBackend.replace(/\/$/, "")}${endpoint}`;
  }

  return endpoint;
}

export async function responseJsonOrEmpty(response) {
  return response.json().catch(() => ({}));
}
