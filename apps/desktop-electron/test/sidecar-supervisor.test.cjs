const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const { verifyHealth } = require("../src/sidecar-supervisor.cjs");
const { EventEmitter } = require("node:events");
const { PassThrough } = require("node:stream");
const path = require("node:path");
const net = require("node:net");
const crypto = require("node:crypto");

const session = {
  protocol_version: "desktop-loopback/1",
  instance_id: "instance-abc",
  nonce: "nonce-abc",
  child_pid: 321,
  expires_at: "2026-07-10T18:00:00+08:00",
};

function health(overrides = {}) {
  return {
    status: "ok",
    desktop_session: {
      protocol_version: session.protocol_version,
      instance_id: session.instance_id,
      nonce: session.nonce,
      child_pid: session.child_pid,
      session_expires_at: session.expires_at,
      status: "ready",
      auth_required: true,
      renderer_secret_access: false,
      ...overrides,
    },
  };
}

test("accepts only an exact ready sidecar health binding", () => {
  assert.equal(verifyHealth(session, health()), true);
});

test("health rejects a different current credential fingerprint", () => {
  const active = { ...session, secret: "private-candidate" };
  assert.throws(() => verifyHealth(active, health({ session_fingerprint: "wrong" })), /desktop_session_health_mismatch/);
  assert.equal(verifyHealth(active, health({ session_fingerprint: crypto.createHash("sha256").update(active.secret).digest("hex") })), true);
});

function renewalFixture({ responseLost = false, rejectRotation = false } = {}) {
  let now = Date.parse("2026-09-25T00:00:00.000Z");
  const timerJobs = new Map();
  let timerId = 0;
  let starts = 0;
  let stops = 0;
  let serverSession = null;
  const failures = [];
  const rotations = [];
  const child = new EventEmitter();
  child.pid = 8751; child.exitCode = null;
  child.stdout = new PassThrough(); child.stderr = new PassThrough();
  const { SidecarSupervisor } = require("../src/sidecar-supervisor.cjs");
  const supervisor = new SidecarSupervisor({
    rootDir: ".", pythonPath: "python",
    now: () => now,
    setTimer: (callback, delay) => { const id = ++timerId; timerJobs.set(id, { at: now + delay, callback }); return id; },
    clearTimer: (id) => timerJobs.delete(id),
    spawnChild: () => { starts += 1; return child; },
    terminateTree: async (target) => { stops += 1; target.exitCode = 0; target.emit("exit", 0); },
    onRenewalFailure: (event) => failures.push(event),
    request: async (url, headers, options = {}) => {
      if (url.endsWith("/rotate")) {
        const body = options.body;
        rotations.push({ body, header: headers["X-Chriptmas-Desktop-Session"] });
        if (rejectRotation) return { status: 503, body: {} };
        serverSession = { ...serverSession, secret: body.next_secret, expires_at: body.next_expires_at };
        if (responseLost) throw new Error("simulated_lost_response");
        return { status: 200, body: { status: "rotated", instance_id: body.instance_id, rotation_id: body.rotation_id, session_expires_at: body.next_expires_at } };
      }
      if (url.endsWith("/api/health") && headers["X-Chriptmas-Desktop-Session"] === serverSession?.secret) {
        return { status: 200, body: { status: "ok", desktop_session: {
          protocol_version: serverSession.protocol_version, instance_id: serverSession.instance_id,
          nonce: serverSession.nonce, child_pid: serverSession.child_pid,
          session_expires_at: serverSession.expires_at, status: "ready", auth_required: true,
          renderer_secret_access: false,
          session_fingerprint: crypto.createHash("sha256").update(serverSession.secret).digest("hex"),
        }, runtime_roots: { schema_version: "runtime-roots-v1", version: 1,
          revision: supervisor.runtimeRootConfig.revision,
          roots: Object.fromEntries(["vault", "model", "media"].map((role) => [role, { readable: true }])) } } };
      }
      return { status: 403, body: {} };
    },
  });
  supervisor.waitForHealth = async () => {};
  return {
    supervisor, failures, rotations,
    get starts() { return starts; }, get stops() { return stops; },
    async start() { const active = await supervisor.start(); serverSession = active; return active; },
    async advance(milliseconds) {
      const target = now + milliseconds;
      while (true) {
        const next = [...timerJobs].sort((a, b) => a[1].at - b[1].at)[0];
        if (!next || next[1].at > target) break;
        timerJobs.delete(next[0]); now = next[1].at; next[1].callback();
        await new Promise((resolve) => setImmediate(resolve));
      }
      now = target;
    },
  };
}

test("rotates before eight hours and preserves the sidecar through a second session", async () => {
  const value = renewalFixture();
  const first = await value.start();
  await value.advance(8 * 60 * 60 * 1000 + 60 * 1000);
  assert.equal(value.rotations.length, 1);
  assert.notEqual(value.supervisor.session.secret, first.secret);
  assert.equal(value.supervisor.session.instance_id, first.instance_id);
  assert.equal(value.supervisor.session.child_pid, first.child_pid);
  assert.ok(value.supervisor.usableSession);
  await value.advance(8 * 60 * 60 * 1000);
  assert.equal(value.rotations.length, 2);
  assert.equal(value.starts, 1);
  assert.equal(value.stops, 0);
  await value.supervisor.stop();
});

test("lost rotation response is recovered with candidate health and keeps the same sidecar", async () => {
  const value = renewalFixture({ responseLost: true });
  const first = await value.start();
  await value.advance(7 * 60 * 60 * 1000 + 45 * 60 * 1000);
  assert.notEqual(value.supervisor.session.secret, first.secret);
  assert.equal(value.failures.length, 0);
  assert.equal(value.starts, 1);
  await value.supervisor.stop();
});

test("expired renewal failure blocks new sessions without terminating in-flight sidecar", async () => {
  const value = renewalFixture({ rejectRotation: true });
  await value.start();
  await value.advance(8 * 60 * 60 * 1000 + 1000);
  assert.equal(value.supervisor.usableSession, null);
  assert.equal(value.failures.some((event) => event.expired === true), true);
  assert.equal(value.starts, 1);
  assert.equal(value.stops, 0);
  await value.supervisor.stop();
});

test("stopping during rotation ignores a late successful response", async () => {
  const value = renewalFixture();
  await value.start();
  const before = value.supervisor.session;
  let release;
  value.supervisor.request = async () => new Promise((resolve) => {
    const candidate = value.supervisor.pendingRotation;
    release = () => resolve({ status: 200, body: { status: "rotated", instance_id: before.instance_id, rotation_id: candidate.rotation_id, session_expires_at: candidate.next.expires_at } });
  });
  const pending = value.supervisor.renewSession();
  await value.supervisor.stop();
  release();
  await assert.rejects(pending, /desktop_session_rotation_cancelled/);
  assert.equal(value.supervisor.session, null);
});

test("runtime-root observation and probe require the resolved roots and revision", () => {
  const { normalizeRuntimeRootConfig, verifyRuntimeRootProbe } = require("../src/sidecar-supervisor.cjs");
  const root = path.resolve(process.cwd());
  const config = normalizeRuntimeRootConfig({
    version: 1,
    revision: "pointer:123:456",
    roots: { vault: root, model: root, media: root },
  }, root);
  const observed = {
    schema_version: "runtime-roots-v1", version: 1, revision: "pointer:123:456",
    roots: {
      vault: { path: root, readable: true }, model: { path: root, readable: true }, media: { path: root, readable: true },
    },
  };
  assert.equal(verifyHealth(session, { ...health(), runtime_roots: observed }, config), true);
  assert.equal(verifyRuntimeRootProbe({ ...observed, probes: {
    vault: { read: true, write: true, cleanup: true },
    model: { read: true, write: true, cleanup: true },
    media: { read: true, write: true, cleanup: true },
  } }, config), true);
  assert.throws(() => verifyRuntimeRootProbe({ ...observed, probes: {} }, config), /desktop_runtime_root_probe_mismatch/);
});

test("sidecar rejects an unsupported independent model or media root before launch", () => {
  const { normalizeRuntimeRootConfig } = require("../src/sidecar-supervisor.cjs");
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-root-layout-"));
  const root = path.join(temporary, "vault");
  const independent = path.join(temporary, "models");
  fs.mkdirSync(root); fs.mkdirSync(independent);
  assert.throws(() => normalizeRuntimeRootConfig({
    version: 1, revision: "pointer:1:1",
    roots: { vault: root, model: independent, media: root },
  }, root), /sidecar_runtime_root_layout_unsupported/);
  fs.rmSync(temporary, { recursive: true, force: true });
});

test("sidecar canonicalizes equivalent real filesystem root spellings", () => {
  const { normalizeRuntimeRootConfig, verifyRuntimeRootProbe } = require("../src/sidecar-supervisor.cjs");
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-root-alias-"));
  const vault = path.join(temporary, "vault");
  const alias = path.join(temporary, "vault-alias");
  fs.mkdirSync(vault);
  try {
    fs.symlinkSync(vault, alias, process.platform === "win32" ? "junction" : "dir");
    const config = normalizeRuntimeRootConfig({
      version: 1, revision: "pointer:2:2",
      roots: { vault, model: alias, media: path.join(vault, ".") },
    }, vault);
    assert.equal(config.roots.vault, config.roots.model);
    assert.equal(config.roots.vault, config.roots.media);
    assert.equal(verifyRuntimeRootProbe({
      schema_version: "runtime-roots-v1", version: 1, revision: "pointer:2:2",
      roots: {
        vault: { path: alias, readable: true }, model: { path: alias, readable: true }, media: { path: alias, readable: true },
      },
      probes: {
        vault: { read: true, write: true, cleanup: true },
        model: { read: true, write: true, cleanup: true },
        media: { read: true, write: true, cleanup: true },
      },
    }, config), true);
  } catch (error) {
    if (error?.code === "EPERM" || error?.code === "EACCES") return;
    throw error;
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
});

test("implicit development root uses the same canonical path comparison", () => {
  const { normalizeRuntimeRootConfig, sameCanonicalPath } = require("../src/sidecar-supervisor.cjs");
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-root-implicit-"));
  const vault = path.join(temporary, "vault");
  const alias = path.join(temporary, "vault-alias");
  fs.mkdirSync(vault);
  try {
    fs.symlinkSync(vault, alias, process.platform === "win32" ? "junction" : "dir");
    const config = normalizeRuntimeRootConfig(null, alias);
    assert.equal(sameCanonicalPath(config.roots.vault, vault), true);
  } catch (error) {
    if (error?.code === "EPERM" || error?.code === "EACCES") return;
    throw error;
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
});

for (const field of ["protocol_version", "instance_id", "nonce", "child_pid", "session_expires_at"]) {
  test(`rejects mismatched ${field}`, () => {
    assert.throws(() => verifyHealth(session, health({ [field]: "mismatch" })), /desktop_session_health_mismatch/);
  });
}

test("rejects missing auth or renderer secret exposure", () => {
  assert.throws(() => verifyHealth(session, health({ auth_required: false })), /desktop_session_health_mismatch/);
  assert.throws(() => verifyHealth(session, health({ renderer_secret_access: true })), /desktop_session_health_mismatch/);
});

test("packaged startup keeps the user-visible forty-five second budget", () => {
  const { DEFAULT_STARTUP_TIMEOUT_MS, PACKAGED_STARTUP_TIMEOUT_MS } = require("../src/sidecar-supervisor.cjs");
  assert.equal(DEFAULT_STARTUP_TIMEOUT_MS, 45000);
  assert.equal(PACKAGED_STARTUP_TIMEOUT_MS, 45000);
});

test("rejects an invalid requested restart port", () => {
  const { SidecarSupervisor } = require("../src/sidecar-supervisor.cjs");
  assert.throws(
    () => new SidecarSupervisor({ rootDir: ".", pythonPath: "python", requestedPort: 0 }),
    /sidecar_requested_port_invalid/,
  );
});

test("starts on the requested renderer-visible restart port", async () => {
  const reservation = net.createServer();
  const requestedPort = await new Promise((resolve, reject) => {
    reservation.once("error", reject);
    reservation.listen({ host: "127.0.0.1", port: 0 }, () => resolve(reservation.address().port));
  });
  await new Promise((resolve, reject) => reservation.close((error) => error ? reject(error) : resolve()));
  const child = new EventEmitter();
  child.pid = 654;
  child.exitCode = null;
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  const { SidecarSupervisor } = require("../src/sidecar-supervisor.cjs");
  const supervisor = new SidecarSupervisor({
    rootDir: ".",
    pythonPath: "python",
    requestedPort,
    spawnChild: () => child,
    terminateTree: async (target) => { target.exitCode = 0; target.emit("exit", 0); },
  });
  supervisor.waitForHealth = async () => {};

  const session = await supervisor.start();
  assert.equal(session.origin, `http://127.0.0.1:${requestedPort}`);
  await supervisor.stop();
});

test("unexpected child exit is observable and keeps its exit diagnostic", async () => {
  const unexpected = [];
  const logs = [];
  const child = new EventEmitter();
  child.pid = 321;
  child.exitCode = null;
  const { SidecarSupervisor } = require("../src/sidecar-supervisor.cjs");
  const supervisor = new SidecarSupervisor({
    rootDir: ".",
    pythonPath: "python",
    spawnChild: () => child,
    log: (message) => logs.push(message),
    onUnexpectedExit: (event) => unexpected.push(event),
  });
  supervisor.waitForHealth = async () => {};

  const session = await supervisor.start();
  child.exitCode = 7;
  child.emit("exit", 7);

  assert.deepEqual(logs, ["sidecar exited (7)"]);
  assert.deepEqual(unexpected, [{ code: 7, origin: session.origin }]);
});

test("intentional forced shutdown is logged as stopped rather than failed", async () => {
  const unexpected = [];
  const logs = [];
  const child = new EventEmitter();
  child.pid = 432;
  child.exitCode = null;
  const { SidecarSupervisor } = require("../src/sidecar-supervisor.cjs");
  const supervisor = new SidecarSupervisor({
    rootDir: ".",
    pythonPath: "python",
    spawnChild: () => child,
    terminateTree: async (target) => {
      target.exitCode = 1;
      target.emit("exit", 1);
    },
    log: (message) => logs.push(message),
    onUnexpectedExit: (event) => unexpected.push(event),
  });
  supervisor.waitForHealth = async () => {};

  await supervisor.start();
  await supervisor.stop();

  assert.deepEqual(logs, ["sidecar stopped"]);
  assert.deepEqual(unexpected, []);
});

test("companion runtime environment accepts only fixed non-secret keys", () => {
  const { validateCompanionEnv } = require("../src/sidecar-supervisor.cjs");
  assert.deepEqual(validateCompanionEnv({ CHRIPTMAS_COMPANION_MODE: "development" }), { CHRIPTMAS_COMPANION_MODE: "development" });
  assert.throws(() => validateCompanionEnv({ CHRIPTMAS_DESKTOP_SESSION_SECRET: "steal" }), /companion_env_invalid/);
  assert.throws(() => validateCompanionEnv({ CHRIPTMAS_COMPANION_MODE: "bad\0mode" }), /companion_env_invalid/);
});

test("mixed media fixture nonce is supervisor-generated and absent by default", async () => {
  const { SidecarSupervisor } = require("../src/sidecar-supervisor.cjs");
  async function spawned(fixture) {
    const child = new EventEmitter(); child.pid = 777; child.exitCode = null;
    child.stdout = new PassThrough(); child.stderr = new PassThrough();
    let options;
    const supervisor = new SidecarSupervisor({
      rootDir: ".", pythonPath: "python", e2eMediaFixture: fixture,
      spawnChild: (_python, _args, value) => { options = value; return child; },
      terminateTree: async (target) => { target.exitCode = 0; target.emit("exit", 0); },
    });
    supervisor.waitForHealth = async () => {};
    await supervisor.start(); await supervisor.stop();
    return options.env;
  }
  const ordinary = await spawned(false);
  const fixture = await spawned(true);
  assert.equal(ordinary.CHRIPTMAS_E2E_MIXED_MEDIA_FIXTURE_NONCE, undefined);
  assert.equal(fixture.CHRIPTMAS_E2E_MIXED_MEDIA_FIXTURE_NONCE, fixture.CHRIPTMAS_DESKTOP_NONCE);
  assert.ok(fixture.CHRIPTMAS_DESKTOP_NONCE.length > 20);
});

test("plugin Hook fault nonce is supervisor-generated and absent by default", async () => {
  const { SidecarSupervisor } = require("../src/sidecar-supervisor.cjs");
  async function spawned(enabled) {
    const child = new EventEmitter(); child.pid = 779; child.exitCode = null;
    child.stdout = new PassThrough(); child.stderr = new PassThrough();
    let options;
    const supervisor = new SidecarSupervisor({
      rootDir: ".", pythonPath: "python", e2ePluginHookFault: enabled,
      spawnChild: (_python, _args, value) => { options = value; return child; },
      terminateTree: async (target) => { target.exitCode = 0; target.emit("exit", 0); },
    });
    supervisor.waitForHealth = async () => {};
    await supervisor.start(); await supervisor.stop();
    return options.env;
  }
  const ordinary = await spawned(false);
  const enabled = await spawned(true);
  assert.equal(ordinary.CHRIPTMAS_E2E_PLUGIN_HOOK_FAULT_NONCE, undefined);
  assert.equal(enabled.CHRIPTMAS_E2E_PLUGIN_HOOK_FAULT_NONCE, enabled.CHRIPTMAS_DESKTOP_NONCE);
});

test("XHS controlled credential fixture nonce is supervisor-generated and absent by default", async () => {
  const { SidecarSupervisor } = require("../src/sidecar-supervisor.cjs");
  async function spawned(enabled) {
    const child = new EventEmitter(); child.pid = 781; child.exitCode = null;
    child.stdout = new PassThrough(); child.stderr = new PassThrough();
    let options;
    const supervisor = new SidecarSupervisor({
      rootDir: ".", pythonPath: "python", e2eXhsControlledCredential: enabled,
      spawnChild: (_python, _args, value) => { options = value; return child; },
      terminateTree: async (target) => { target.exitCode = 0; target.emit("exit", 0); },
    });
    supervisor.waitForHealth = async () => {};
    await supervisor.start(); await supervisor.stop();
    return options.env;
  }
  const ordinary = await spawned(false);
  const enabled = await spawned(true);
  assert.equal(ordinary.CHRIPTMAS_E2E_XHS_CREDENTIAL_FIXTURE_NONCE, undefined);
  assert.equal(enabled.CHRIPTMAS_E2E_XHS_CREDENTIAL_FIXTURE_NONCE, enabled.CHRIPTMAS_DESKTOP_NONCE);
});

test("XHS controlled real OCR nonce requires its independent supervisor mode", async () => {
  const { SidecarSupervisor } = require("../src/sidecar-supervisor.cjs");
  async function spawned(enabled) {
    const child = new EventEmitter(); child.pid = 782; child.exitCode = null;
    child.stdout = new PassThrough(); child.stderr = new PassThrough();
    let options;
    const supervisor = new SidecarSupervisor({
      rootDir: ".", pythonPath: "python", e2eXhsControlledCredential: true,
      e2eXhsControlledRealOcr: enabled,
      spawnChild: (_python, _args, value) => { options = value; return child; },
      terminateTree: async (target) => { target.exitCode = 0; target.emit("exit", 0); },
    });
    supervisor.waitForHealth = async () => {};
    await supervisor.start(); await supervisor.stop();
    return options.env;
  }
  const ordinary = await spawned(false);
  const enabled = await spawned(true);
  assert.equal(ordinary.CHRIPTMAS_E2E_XHS_CREDENTIAL_REAL_OCR_NONCE, undefined);
  assert.equal(enabled.CHRIPTMAS_E2E_XHS_CREDENTIAL_REAL_OCR_NONCE, enabled.CHRIPTMAS_DESKTOP_NONCE);
});

test("uses an opt-in parent stdin watchdog pipe for every supervised sidecar", async () => {
  const child = new EventEmitter(); child.pid = 778; child.exitCode = null;
  child.stdout = new PassThrough(); child.stderr = new PassThrough();
  let requested;
  const { SidecarSupervisor } = require("../src/sidecar-supervisor.cjs");
  const supervisor = new SidecarSupervisor({
    rootDir: ".", pythonPath: "python",
    spawnChild: (executable, args, options) => {
      requested = { executable, args, stdio: options.stdio };
      return child;
    },
    terminateTree: async (target) => { target.exitCode = 0; target.emit("exit", 0); },
  });
  supervisor.waitForHealth = async () => {};

  await supervisor.start();
  await supervisor.stop();

  assert.equal(requested.executable, "python");
  assert.deepEqual(requested.args.slice(-1), ["--parent-stdin-watchdog"]);
  assert.deepEqual(requested.stdio, ["pipe", "pipe", "pipe"]);
});

test("sidecar passes only its validated runtime root contract to the child", async () => {
  const child = new EventEmitter(); child.pid = 783; child.exitCode = null;
  child.stdout = new PassThrough(); child.stderr = new PassThrough();
  const root = path.resolve(process.cwd());
  let environment;
  const { SidecarSupervisor } = require("../src/sidecar-supervisor.cjs");
  const supervisor = new SidecarSupervisor({
    rootDir: root, workingDir: root, pythonPath: "python",
    runtimeRootConfig: { version: 1, revision: "pointer:88:11", roots: { vault: root, model: root, media: root } },
    spawnChild: (_python, _args, options) => { environment = options.env; return child; },
    terminateTree: async (target) => { target.exitCode = 0; target.emit("exit", 0); },
  });
  supervisor.waitForHealth = async () => {};
  await supervisor.start();
  await supervisor.stop();
  assert.equal(environment.CHRIPTMAS_RUNTIME_ROOT_VERSION, "1");
  assert.equal(environment.CHRIPTMAS_RUNTIME_ROOT_REVISION, "pointer:88:11");
  assert.equal(environment.CHRIPTMAS_RUNTIME_VAULT_ROOT, root);
  assert.equal(environment.CHRIPTMAS_RUNTIME_MODEL_ROOT, root);
  assert.equal(environment.CHRIPTMAS_RUNTIME_MEDIA_ROOT, root);
});

test("uses the configured cold-start timeout and continuously drains child output", async () => {
  const child = new EventEmitter();
  child.pid = 654;
  child.exitCode = null;
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  const seenTimeouts = [];
  const { PACKAGED_STARTUP_TIMEOUT_MS, SidecarSupervisor } = require("../src/sidecar-supervisor.cjs");
  const supervisor = new SidecarSupervisor({
    rootDir: ".",
    pythonPath: "python",
    startupTimeoutMs: PACKAGED_STARTUP_TIMEOUT_MS,
    spawnChild: () => child,
    terminateTree: async (target) => {
      target.exitCode = 0;
      target.emit("exit", 0);
    },
  });
  supervisor.waitForHealth = async (timeoutMs) => {
    seenTimeouts.push(timeoutMs);
    child.stdout.write("booting stdout\n");
    child.stderr.write("booting stderr\n");
    await new Promise((resolve) => setImmediate(resolve));
  };

  await supervisor.start();

  assert.deepEqual(seenTimeouts, [PACKAGED_STARTUP_TIMEOUT_MS]);
  assert.match(supervisor.outputTail, /booting stdout/);
  assert.match(supervisor.outputTail, /booting stderr/);
  await supervisor.stop();
});

test("startup exit returns one bounded diagnostic without an unexpected-exit callback", async () => {
  const unexpected = [];
  const logs = [];
  const child = new EventEmitter();
  child.pid = 987;
  child.exitCode = null;
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  const { SidecarSupervisor } = require("../src/sidecar-supervisor.cjs");
  const supervisor = new SidecarSupervisor({
    rootDir: ".",
    pythonPath: "python",
    spawnChild: () => child,
    terminateTree: async () => {},
    log: (message) => logs.push(message),
    onUnexpectedExit: (event) => unexpected.push(event),
  });
  supervisor.waitForHealth = async () => {
    child.stderr.write(`trace:${"x".repeat(9000)}`);
    await new Promise((resolve) => setImmediate(resolve));
    child.exitCode = 1;
    child.emit("exit", 1);
    const error = new Error("sidecar_exited_before_health");
    error.code = "SIDECAR_EXITED_BEFORE_HEALTH";
    throw error;
  };

  await assert.rejects(
    supervisor.start(),
    (error) => error.code === "SIDECAR_EXITED_BEFORE_HEALTH"
      && error.message === "sidecar_startup_failed"
      && error.diagnostic.length === 8192,
  );
  assert.deepEqual(unexpected, []);
  assert.deepEqual(logs, ["sidecar exited (1)"]);
});
