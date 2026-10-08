const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { MODE: CLOCK_MODE, MODE_KEY: CLOCK_MODE_KEY, UTC: CLOCK_UTC, UTC_KEY: CLOCK_UTC_KEY, stripCompanionE2EClockEnv } = require("./companion/clock-e2e-env.cjs");

const PROTOCOL_VERSION = "desktop-loopback/1";
const SESSION_HEADER = "X-Chriptmas-Desktop-Session";
const ROTATION_VERSION = "desktop-session-rotation/1";
const SESSION_LIFETIME_MS = 8 * 60 * 60 * 1000;
const RENEW_BEFORE_MS = 15 * 60 * 1000;
const RENEW_RETRY_MS = 30 * 1000;
const DEFAULT_STARTUP_TIMEOUT_MS = 45000;
const PACKAGED_STARTUP_TIMEOUT_MS = 45000;
const OUTPUT_TAIL_LIMIT = 8192;
const COMPANION_ENV_KEYS = new Set([
  "CHRIPTMAS_COMPANION_MODE",
  "CHRIPTMAS_COMPANION_USER_DATA_ROOT",
  "CHRIPTMAS_COMPANION_REPOSITORY_ROOT",
  "CHRIPTMAS_COMPANION_RESOURCES_ROOT",
  CLOCK_MODE_KEY,
  CLOCK_UTC_KEY,
]);

function randomToken() {
  return crypto.randomBytes(32).toString("base64url");
}

function allocateLoopbackPort(requestedPort = 0) {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen({ host: "127.0.0.1", port: requestedPort }, () => {
      const address = server.address();
      server.close((error) => error ? reject(error) : resolve(address.port));
    });
  });
}

function requestJson(url, headers, { method = "GET", body } = {}) {
  return new Promise((resolve, reject) => {
    const request = http.request(url, { method, headers, timeout: 1200 }, (response) => {
      let body = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => { body += chunk; });
      response.on("end", () => {
        try { resolve({ status: response.statusCode, body: JSON.parse(body) }); }
        catch { reject(new Error("desktop_session_health_invalid_json")); }
      });
    });
    request.on("timeout", () => request.destroy(new Error("desktop_session_health_timeout")));
    request.on("error", reject);
    request.end(body === undefined ? undefined : JSON.stringify(body));
  });
}

function verifyHealth(session, payload, runtimeRootConfig = null) {
  const health = payload?.desktop_session;
  if (!health || payload.status !== "ok" || health.status !== "ready") throw new Error("desktop_session_health_mismatch");
  for (const key of ["protocol_version", "instance_id", "nonce", "child_pid"]) {
    if (health[key] !== session[key]) throw new Error("desktop_session_health_mismatch");
  }
  if (health.session_expires_at !== session.expires_at) throw new Error("desktop_session_health_mismatch");
  if (typeof session.secret === "string" && health.session_fingerprint !== crypto.createHash("sha256").update(session.secret).digest("hex")) {
    throw new Error("desktop_session_health_mismatch");
  }
  if (health.auth_required !== true || health.renderer_secret_access !== false) throw new Error("desktop_session_health_mismatch");
  if (runtimeRootConfig !== null && !sameRuntimeRootObservation(payload?.runtime_roots, runtimeRootConfig)) {
    throw new Error("desktop_runtime_root_observation_mismatch");
  }
  return true;
}

function normalizeRuntimeRootConfig(value, workingDir) {
  if (value == null) {
    if (typeof workingDir !== "string" || !workingDir) throw new TypeError("sidecar_runtime_root_invalid");
    const root = canonicalPath(path.resolve(workingDir));
    if (!root) throw new TypeError("sidecar_runtime_root_invalid");
    return Object.freeze({ version: 1, revision: "implicit:working-dir", roots: Object.freeze({ vault: root, model: root, media: root }) });
  }
  if (!value || typeof value !== "object" || Array.isArray(value) || value.version !== 1
    || typeof value.revision !== "string" || !value.revision || value.revision.length > 256 || value.revision.includes("\0")
    || !value.roots || typeof value.roots !== "object" || Array.isArray(value.roots)
    || Object.keys(value.roots).sort().join(",") !== "media,model,vault") {
    throw new TypeError("sidecar_runtime_root_invalid");
  }
  const roots = Object.freeze(Object.fromEntries(["vault", "model", "media"].map((role) => [role, canonicalPath(value.roots[role])])));
  if (Object.values(roots).some((root) => root === null)) throw new TypeError("sidecar_runtime_root_invalid");
  if (roots.model !== roots.vault || roots.media !== roots.vault) throw new TypeError("sidecar_runtime_root_layout_unsupported");
  return Object.freeze({ version: 1, revision: value.revision, roots });
}

function canonicalPath(value) {
  if (typeof value !== "string" || !value || !path.isAbsolute(value)) return null;
  const resolved = path.resolve(value);
  try {
    return fs.realpathSync.native(resolved);
  } catch {
    // The backend separately requires a directory before it can report a
    // healthy root. Keeping this lexical fallback makes the constructor's
    // validation deterministic when the OS cannot resolve a reparse point.
    return resolved;
  }
}

function sameCanonicalPath(left, right) {
  const normalizedLeft = canonicalPath(left);
  const normalizedRight = canonicalPath(right);
  if (!normalizedLeft || !normalizedRight) return false;
  return process.platform === "win32"
    ? normalizedLeft.toLowerCase() === normalizedRight.toLowerCase()
    : normalizedLeft === normalizedRight;
}

function sameRuntimeRootObservation(observed, config, { requirePaths = false } = {}) {
  if (!observed || typeof observed !== "object" || observed.schema_version !== "runtime-roots-v1"
    || observed.version !== config.version || observed.revision !== config.revision
    || !observed.roots || typeof observed.roots !== "object") return false;
  return ["vault", "model", "media"].every((role) => observed.roots[role]?.readable === true
    && (!requirePaths || sameCanonicalPath(observed.roots[role]?.path, config.roots[role])));
}

function verifyRuntimeRootProbe(payload, config) {
  if (!sameRuntimeRootObservation(payload, config, { requirePaths: true }) || !payload.probes || typeof payload.probes !== "object") {
    throw new Error("desktop_runtime_root_probe_mismatch");
  }
  if (!["vault", "model", "media"].every((role) => payload.probes[role]?.read === true
    && payload.probes[role]?.write === true && payload.probes[role]?.cleanup === true)) {
    throw new Error("desktop_runtime_root_probe_mismatch");
  }
  return true;
}

function terminateProcessTree(child, spawnProcess = spawn) {
  if (!child || child.exitCode !== null) return Promise.resolve();
  if (process.platform !== "win32" || !Number.isInteger(child.pid)) {
    child.kill();
    return Promise.resolve();
  }
  return new Promise((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      resolve();
    };
    const killer = spawnProcess("taskkill", ["/PID", String(child.pid), "/T", "/F"], {
      windowsHide: true,
      stdio: "ignore",
    });
    killer.once("error", () => { child.kill(); finish(); });
    killer.once("exit", finish);
  });
}

class SidecarSupervisor {
  constructor({ rootDir, moduleRoot = path.join(rootDir, "src"), workingDir = rootDir, runtimeRootConfig, pythonPath, requestedPort = null, companionEnv = {}, e2eMediaFixture = false, e2ePluginHookFault = false, e2eXhsControlledCredential = false, e2eXhsControlledRealOcr = false, startupTimeoutMs = DEFAULT_STARTUP_TIMEOUT_MS, log = () => {}, spawnChild = spawn, terminateTree = terminateProcessTree, onUnexpectedExit = () => {}, onRenewalFailure = () => {}, now = () => Date.now(), setTimer = setTimeout, clearTimer = clearTimeout, request = requestJson }) {
    this.rootDir = rootDir;
    this.moduleRoot = moduleRoot;
    this.workingDir = workingDir;
    this.runtimeRootConfig = normalizeRuntimeRootConfig(runtimeRootConfig, workingDir);
    this.pythonPath = pythonPath;
    if (requestedPort !== null && (!Number.isInteger(requestedPort) || requestedPort < 1 || requestedPort > 65535)) {
      throw new TypeError("sidecar_requested_port_invalid");
    }
    this.requestedPort = requestedPort;
    this.companionEnv = validateCompanionEnv(companionEnv);
    this.e2eMediaFixture = e2eMediaFixture === true;
    this.e2ePluginHookFault = e2ePluginHookFault === true;
    this.e2eXhsControlledCredential = e2eXhsControlledCredential === true;
    this.e2eXhsControlledRealOcr = e2eXhsControlledRealOcr === true;
    if (!Number.isInteger(startupTimeoutMs) || startupTimeoutMs < 1000 || startupTimeoutMs > 300000) {
      throw new TypeError("sidecar_startup_timeout_invalid");
    }
    this.startupTimeoutMs = startupTimeoutMs;
    this.log = log;
    this.spawnChild = spawnChild;
    this.terminateTree = terminateTree;
    this.onUnexpectedExit = onUnexpectedExit;
    this.onRenewalFailure = onRenewalFailure;
    this.now = now;
    this.setTimer = setTimer;
    this.clearTimer = clearTimer;
    this.request = request;
    this.renewalTimer = null;
    this.renewalPromise = null;
    this.pendingRotation = null;
    this.renewalFailureNotified = false;
    this.lifecycleEpoch = 0;
    this.child = null;
    this.session = null;
    this.stopping = false;
    this.starting = false;
    this.outputTail = "";
  }

  async start() {
    if (this.child) return this.session;
    this.lifecycleEpoch += 1;
    this.starting = true;
    this.stopping = false;
    this.outputTail = "";
    const port = await allocateLoopbackPort(this.requestedPort || 0);
    const instance_id = randomToken();
    const nonce = randomToken();
    const secret = randomToken();
    const expires_at = new Date(this.now() + SESSION_LIFETIME_MS).toISOString();
    const origin = `http://127.0.0.1:${port}`;
    const env = {
      ...stripCompanionE2EClockEnv(process.env),
      PYTHONPATH: this.moduleRoot,
      PYTHONDONTWRITEBYTECODE: "1",
      CHRIPTMAS_APP_ROOT: this.workingDir,
      CHRIPTMAS_RUNTIME_ROOT_VERSION: String(this.runtimeRootConfig.version),
      CHRIPTMAS_RUNTIME_ROOT_REVISION: this.runtimeRootConfig.revision,
      CHRIPTMAS_RUNTIME_VAULT_ROOT: this.runtimeRootConfig.roots.vault,
      CHRIPTMAS_RUNTIME_MODEL_ROOT: this.runtimeRootConfig.roots.model,
      CHRIPTMAS_RUNTIME_MEDIA_ROOT: this.runtimeRootConfig.roots.media,
      ...this.companionEnv,
      CHRIPTMAS_DESKTOP_SESSION_MODE: "desktop_production",
      CHRIPTMAS_DESKTOP_SESSION_SECRET: secret,
      CHRIPTMAS_DESKTOP_INSTANCE_ID: instance_id,
      CHRIPTMAS_DESKTOP_NONCE: nonce,
      CHRIPTMAS_DESKTOP_PROTOCOL_VERSION: PROTOCOL_VERSION,
      CHRIPTMAS_DESKTOP_SESSION_EXPIRES_AT: expires_at,
      CHRIPTMAS_DESKTOP_ALLOWED_ORIGIN: origin,
    };
    if (this.e2eMediaFixture) env.CHRIPTMAS_E2E_MIXED_MEDIA_FIXTURE_NONCE = nonce;
    if (this.e2ePluginHookFault) env.CHRIPTMAS_E2E_PLUGIN_HOOK_FAULT_NONCE = nonce;
    if (this.e2eXhsControlledCredential) env.CHRIPTMAS_E2E_XHS_CREDENTIAL_FIXTURE_NONCE = nonce;
    if (this.e2eXhsControlledRealOcr) env.CHRIPTMAS_E2E_XHS_CREDENTIAL_REAL_OCR_NONCE = nonce;
    const child = this.spawnChild(this.pythonPath, [
      "-m", "backend.api.server", "--host", "127.0.0.1", "--port", String(port), "--parent-stdin-watchdog",
    ], {
      cwd: this.workingDir,
      env,
      // The sidecar reads this private pipe only when the explicit command-line
      // watchdog flag is present. If Electron is forcibly terminated without
      // taskkill /T, Windows closes the owner end and the sidecar can perform
      // its ordinary ASGI shutdown instead of remaining orphaned.
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    });
    this.child = child;
    this.session = { protocol_version: PROTOCOL_VERSION, instance_id, nonce, secret, expires_at, origin, child_pid: child.pid };
    this.captureOutput(child.stdout);
    this.captureOutput(child.stderr);
    child.once("exit", (code) => {
      const intentional = this.stopping;
      this.log(intentional ? "sidecar stopped" : `sidecar exited (${code ?? "signal"})`);
      const unexpected = !intentional && !this.starting;
      this.cancelRenewal();
      this.child = null;
      if (unexpected) this.onUnexpectedExit({ code, origin: this.session?.origin || null });
    });
    try {
      await this.waitForHealth(this.startupTimeoutMs);
      this.starting = false;
      this.scheduleRenewal();
      return this.session;
    } catch (error) {
      const diagnostic = this.outputTail.trim();
      await this.stop();
      const wrapped = new Error(error?.code === "SIDECAR_STARTUP_TIMEOUT"
        ? "sidecar_startup_timeout"
        : "sidecar_startup_failed");
      wrapped.code = error?.code || "SIDECAR_STARTUP_FAILED";
      wrapped.cause = error;
      wrapped.diagnostic = diagnostic;
      throw wrapped;
    }
  }

  captureOutput(stream) {
    if (!stream || typeof stream.on !== "function") return;
    stream.on("data", (chunk) => {
      this.outputTail = `${this.outputTail}${String(chunk)}`.slice(-OUTPUT_TAIL_LIMIT);
    });
  }

  async waitForHealth(timeoutMs = 45000) {
    const deadline = Date.now() + timeoutMs;
    let lastError = null;
    while (Date.now() < deadline && this.child) {
      try {
        const response = await this.request(`${this.session.origin}/api/health`, { [SESSION_HEADER]: this.session.secret });
        if (response.status !== 200) throw new Error("desktop_session_health_http_failed");
        verifyHealth(this.session, response.body, this.runtimeRootConfig);
        const probe = await this.request(`${this.session.origin}/api/desktop/runtime-roots/verify`, { [SESSION_HEADER]: this.session.secret }, { method: "POST" });
        if (probe.status !== 200) throw new Error("desktop_runtime_root_probe_failed");
        verifyRuntimeRootProbe(probe.body, this.runtimeRootConfig);
        return;
      } catch (error) { lastError = error; await new Promise((resolve) => setTimeout(resolve, 150)); }
    }
    if (!this.child) {
      const error = new Error("sidecar_exited_before_health");
      error.code = "SIDECAR_EXITED_BEFORE_HEALTH";
      throw error;
    }
    const error = new Error("sidecar_startup_timeout");
    error.code = "SIDECAR_STARTUP_TIMEOUT";
    error.cause = lastError;
    throw error;
  }

  async stop() {
    this.stopping = true;
    this.starting = false;
    this.cancelRenewal();
    const child = this.child;
    this.child = null;
    this.session = null;
    if (!child || child.exitCode !== null) return;
    await new Promise((resolve) => {
      const timeout = setTimeout(resolve, 5000);
      child.once("exit", () => { clearTimeout(timeout); resolve(); });
      Promise.resolve(this.terminateTree(child)).catch(() => child.kill());
    });
  }

  get usableSession() {
    return this.session && this.now() < Date.parse(this.session.expires_at) ? this.session : null;
  }

  cancelRenewal() {
    this.lifecycleEpoch += 1;
    if (this.renewalTimer !== null) this.clearTimer(this.renewalTimer);
    this.renewalTimer = null;
    this.pendingRotation = null;
  }

  scheduleRenewal(delay = null) {
    if (this.renewalTimer !== null) this.clearTimer(this.renewalTimer);
    if (!this.child || !this.session || this.stopping) return;
    const remaining = Date.parse(this.session.expires_at) - this.now();
    if (remaining <= 0) {
      this.reportRenewalFailure(true);
      return;
    }
    const wait = delay === null ? Math.max(0, remaining - RENEW_BEFORE_MS) : Math.min(delay, remaining);
    this.renewalTimer = this.setTimer(() => {
      this.renewalTimer = null;
      this.renewSession().catch(() => {});
    }, wait);
    this.renewalTimer?.unref?.();
  }

  reportRenewalFailure(expired) {
    if (this.renewalFailureNotified && !expired) return;
    if (this.renewalFailureNotified === "expired") return;
    this.renewalFailureNotified = expired ? "expired" : true;
    this.onRenewalFailure({ expired, retry: () => this.renewSession() });
  }

  renewSession() {
    if (this.renewalPromise) return this.renewalPromise;
    const pending = this.renewSessionOnce();
    this.renewalPromise = pending;
    pending.finally(() => { if (this.renewalPromise === pending) this.renewalPromise = null; }).catch(() => {});
    return pending;
  }

  async renewSessionOnce() {
    const old = this.session;
    const epoch = this.lifecycleEpoch;
    if (!old || !this.child || this.stopping || this.now() >= Date.parse(old.expires_at)) {
      this.reportRenewalFailure(true);
      throw new Error("desktop_session_expired");
    }
    if (!this.pendingRotation) {
      const next = { ...old, secret: randomToken(), expires_at: new Date(this.now() + SESSION_LIFETIME_MS).toISOString() };
      this.pendingRotation = { rotation_id: randomToken(), next };
    }
    const { rotation_id, next } = this.pendingRotation;
    const signature = crypto.createHmac("sha256", old.secret).update([
      ROTATION_VERSION, old.instance_id, rotation_id, next.secret, next.expires_at,
    ].join("\n")).digest("hex");
    const body = { version: ROTATION_VERSION, instance_id: old.instance_id, rotation_id, next_secret: next.secret, next_expires_at: next.expires_at, signature };
    let rotated = false;
    try {
      const response = await this.request(`${old.origin}/api/desktop/session/rotate`, {
        [SESSION_HEADER]: old.secret, "Content-Type": "application/json",
      }, { method: "POST", body });
      rotated = response.status === 200 && response.body?.status === "rotated"
        && response.body?.instance_id === old.instance_id && response.body?.rotation_id === rotation_id
        && response.body?.session_expires_at === next.expires_at;
    } catch { /* A lost response can follow a successful server-side swap. */ }
    if (!rotated) {
      try {
        const health = await this.request(`${next.origin}/api/health`, { [SESSION_HEADER]: next.secret });
        if (health.status === 200) rotated = verifyHealth(next, health.body, this.runtimeRootConfig);
      } catch { /* Keep the same candidate for a retry while the old session is valid. */ }
    }
    if (epoch !== this.lifecycleEpoch || this.stopping || this.child?.pid !== old.child_pid || this.session !== old) {
      throw new Error("desktop_session_rotation_cancelled");
    }
    if (!rotated) {
      const expired = this.now() >= Date.parse(old.expires_at);
      this.reportRenewalFailure(expired);
      if (!expired) this.scheduleRenewal(RENEW_RETRY_MS);
      throw new Error("desktop_session_rotation_failed");
    }
    this.session = next;
    this.pendingRotation = null;
    this.renewalFailureNotified = false;
    this.scheduleRenewal();
    return next;
  }
}

function validateCompanionEnv(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("companion_env_invalid");
  const output = {};
  for (const [key, entry] of Object.entries(value)) {
    if (!COMPANION_ENV_KEYS.has(key) || typeof entry !== "string" || !entry || entry.length > 2048 || entry.includes("\0")) {
      throw new Error("companion_env_invalid");
    }
    output[key] = entry;
  }
  const hasClockMode = Object.hasOwn(output, CLOCK_MODE_KEY);
  const hasClockUtc = Object.hasOwn(output, CLOCK_UTC_KEY);
  if (hasClockMode !== hasClockUtc || (hasClockMode && (output[CLOCK_MODE_KEY] !== CLOCK_MODE || !CLOCK_UTC.test(output[CLOCK_UTC_KEY])))) {
    throw new Error("companion_env_invalid");
  }
  return Object.freeze(output);
}

module.exports = { COMPANION_ENV_KEYS, DEFAULT_STARTUP_TIMEOUT_MS, PACKAGED_STARTUP_TIMEOUT_MS, PROTOCOL_VERSION, SESSION_HEADER, SidecarSupervisor, normalizeRuntimeRootConfig, sameCanonicalPath, terminateProcessTree, validateCompanionEnv, verifyHealth, verifyRuntimeRootProbe };
