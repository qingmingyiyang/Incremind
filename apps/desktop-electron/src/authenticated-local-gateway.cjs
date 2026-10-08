"use strict";

const ALLOWED_METHODS = new Set(["GET", "POST"]);
const ERROR_CODE_PATTERN = /^[a-z][a-z0-9_]{2,80}$/;
const MIN_TIMEOUT_MS = 100;
const MAX_TIMEOUT_MS = 120_000;

function validSession(session) {
  if (!session || typeof session.origin !== "string" || typeof session.secret !== "string" || session.secret.length === 0) return null;
  try {
    const parsed = new URL(session.origin);
    if (parsed.protocol !== "http:" || parsed.hostname !== "127.0.0.1" || parsed.origin !== session.origin) return null;
    return { origin: parsed.origin, secret: session.secret };
  } catch {
    return null;
  }
}

function validateRequest({ method, pathname, timeoutMs, unavailableError }) {
  const normalizedMethod = String(method || "GET").toUpperCase();
  if (!ALLOWED_METHODS.has(normalizedMethod)) throw new Error("local_gateway_method_invalid");
  let decodedPathname = null;
  try { decodedPathname = decodeURIComponent(pathname); } catch {}
  const hasTraversal = decodedPathname?.split("/").some((segment) => segment === "." || segment === "..") === true;
  if (typeof pathname !== "string" || decodedPathname === null || !pathname.startsWith("/api/") || pathname.includes("://") || /[?#]/.test(pathname) || decodedPathname.includes("\\") || /[\u0000-\u001f\u007f]/.test(decodedPathname) || hasTraversal) {
    throw new Error("local_gateway_path_invalid");
  }
  if (!Number.isInteger(timeoutMs) || timeoutMs < MIN_TIMEOUT_MS || timeoutMs > MAX_TIMEOUT_MS) throw new Error("local_gateway_timeout_invalid");
  if (typeof unavailableError !== "string" || !ERROR_CODE_PATTERN.test(unavailableError)) throw new Error("local_gateway_error_code_invalid");
  return normalizedMethod;
}

class AuthenticatedLocalGateway {
  #sessionProvider;
  #fetch;
  #createTimeoutSignal;
  #sessionHeader;

  constructor({ sessionProvider, fetchImpl = globalThis.fetch, createTimeoutSignal = (milliseconds) => AbortSignal.timeout(milliseconds), sessionHeader } = {}) {
    if (typeof sessionProvider !== "function" || typeof fetchImpl !== "function" || typeof createTimeoutSignal !== "function" || typeof sessionHeader !== "string" || !sessionHeader) {
      throw new Error("authenticated_local_gateway_options_invalid");
    }
    this.#sessionProvider = sessionProvider;
    this.#fetch = fetchImpl;
    this.#createTimeoutSignal = createTimeoutSignal;
    this.#sessionHeader = sessionHeader;
  }

  isAvailable() {
    try {
      return validSession(this.#sessionProvider()) !== null;
    } catch {
      return false;
    }
  }

  async requestJson({ method = "GET", pathname, body, timeoutMs = 5000, unavailableError, nullableJson = false, parseErrorJson = false } = {}) {
    const normalizedMethod = validateRequest({ method, pathname, timeoutMs, unavailableError });
    const active = validSession(this.#sessionProvider());
    if (!active) throw new Error(unavailableError);
    const headers = { Accept: "application/json", [this.#sessionHeader]: active.secret };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const response = await this.#fetch(`${active.origin}${pathname}`, {
      method: normalizedMethod,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: this.#createTimeoutSignal(timeoutMs),
    });
    let payload = null;
    if (response.ok === true || parseErrorJson) {
      if (nullableJson) {
        try { payload = await response.json(); } catch { payload = null; }
      } else {
        payload = await response.json();
      }
    }
    return Object.freeze({ ok: response.ok === true, status: response.status, payload });
  }
}

module.exports = { AuthenticatedLocalGateway };
