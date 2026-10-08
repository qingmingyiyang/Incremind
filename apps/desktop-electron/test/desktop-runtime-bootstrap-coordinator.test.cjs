const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");

const { DesktopRuntimeBootstrapCoordinator } = require("../src/desktop-runtime-bootstrap-coordinator.cjs");

function fixture({ packaged = false, vault = { mode: "formal", workingDir: "C:\\vault" }, vaultSequence = null, rootMigrationFactory = null, runtimeRootConfig = { version: 1, revision: "pointer:1:2", roots: { vault: "C:\\vault", model: "C:\\vault", media: "C:\\vault" } }, settingsExist = false, e2eMediaFixture = false, e2ePluginHookFault = false, e2eXhsControlledCredential = false, e2eXhsControlledRealOcr = false, spawnSyncImpl } = {}) {
  const events = [];
  const processEnv = {};
  let supervisorOptions;
  const supervisorOptionsHistory = [];
  let recoveryOptions;
  class Supervisor {
    constructor(options) {
      supervisorOptions = options;
      supervisorOptionsHistory.push(options);
      this.session = null;
      this.child = null;
      events.push("supervisor:create");
    }
    async start() {
      events.push("supervisor:start");
      this.child = { pid: 42 };
      this.session = { origin: "http://127.0.0.1:3210", secret: "secret" };
      return this.session;
    }
    async stop() { events.push("supervisor:stop"); this.child = null; }
  }
  class Recovery {
    constructor(options) { recoveryOptions = options; events.push("recovery:create"); }
    async recoverPending() { events.push("recovery:run"); }
  }
  const fsApi = {
    existsSync: (target) => { events.push(["exists", target]); return settingsExist; },
    mkdirSync: (target, options) => events.push(["mkdir", target, options]),
    copyFileSync: (source, target) => events.push(["copy", source, target]),
  };
  let vaultCall = 0;
  const coordinator = new DesktopRuntimeBootstrapCoordinator({
    app: { isPackaged: packaged, getPath: (name) => name === "userData" ? "C:\\user-data" : "" },
    repositoryRoot: "C:\\repo",
    resourcesRootProvider: () => "C:\\resources",
    platform: "win32",
    processEnv,
    SidecarSupervisor: Supervisor,
    VaultRecoveryController: Recovery,
    resolvePackagedVaultRoot: (request) => { events.push(["vault", request]); return vaultSequence ? vaultSequence[Math.min(vaultCall++, vaultSequence.length - 1)] : vault; },
    createRootMigrationController: rootMigrationFactory,
    resolveRuntimeRootConfig: () => runtimeRootConfig,
    packagedStartupTimeoutMs: 45000,
    resolveCompanionEnv: () => ({ CHRIPTMAS_COMPANION_CLOCK_MODE: "fixed" }),
    e2eMediaFixture,
    e2ePluginHookFault,
    e2eXhsControlledCredential,
    e2eXhsControlledRealOcr,
    onVaultConflict: async () => { events.push("vault:conflict"); },
    onUnexpectedExit: () => events.push("unexpected"),
    installRequestAuthentication: () => events.push("auth"),
    installRendererSecurityPolicies: () => events.push("security"),
    startRuntimeControllers: () => events.push("runtime"),
    log: (message) => events.push(["log", message]),
    fsApi,
    pathApi: path.win32,
    ...(spawnSyncImpl ? { spawnSyncImpl } : {}),
  });
  return {
    coordinator,
    events,
    processEnv,
    get supervisorOptions() { return supervisorOptions; },
    supervisorOptionsHistory,
    get recoveryOptions() { return recoveryOptions; },
  };
}

test("development bootstrap preserves recovery startup authentication and runtime order", async () => {
  const value = fixture();
  const first = value.coordinator.start();
  assert.equal(value.coordinator.start(), first);
  const session = await first;

  assert.deepEqual(value.events, [
    ["mkdir", "C:\\repo\\runtime", { recursive: true }],
    ["exists", "C:\\repo\\runtime\\config\\settings.toml"],
    ["mkdir", "C:\\repo\\runtime\\config", { recursive: true }],
    ["copy", "C:\\repo\\config\\settings.toml", "C:\\repo\\runtime\\config\\settings.toml"],
    "supervisor:create",
    "recovery:create",
    "recovery:run",
    "supervisor:start",
    "auth",
    "security",
    "runtime",
  ]);
  assert.equal(value.coordinator.session, session);
  assert.equal(value.coordinator.recovery !== null, true);
  assert.equal(value.supervisorOptions.rootDir, "C:\\repo");
  assert.equal(value.supervisorOptions.moduleRoot, "C:\\repo\\src");
  assert.equal(value.supervisorOptions.workingDir, "C:\\repo\\runtime");
  assert.equal(value.supervisorOptions.pythonPath, "C:\\repo\\python-runtime\\python.exe");
  assert.equal(value.supervisorOptions.startupTimeoutMs, undefined);
  assert.deepEqual(value.supervisorOptions.companionEnv, {
    CHRIPTMAS_COMPANION_MODE: "development",
    CHRIPTMAS_COMPANION_USER_DATA_ROOT: "C:\\user-data",
    CHRIPTMAS_COMPANION_REPOSITORY_ROOT: "C:\\repo",
    CHRIPTMAS_COMPANION_RESOURCES_ROOT: "C:\\repo",
    CHRIPTMAS_COMPANION_CLOCK_MODE: "fixed",
  });
  assert.equal(value.processEnv.CHRIPTMAS_DESKTOP_BACKEND_ORIGIN, session.origin);
});

test("root migration restart reuses the renderer-visible loopback port", async () => {
  const value = fixture();
  await value.coordinator.start();
  const result = await value.coordinator.runRootMigrationExclusive(async (probe) => {
    probe.assertSourceQuiescent();
    return { status: "switched" };
  });

  assert.equal(result.session.origin, "http://127.0.0.1:3210");
  assert.deepEqual(value.supervisorOptionsHistory.map((options) => options.requestedPort), [null, 3210]);
  assert.equal(value.processEnv.CHRIPTMAS_DESKTOP_BACKEND_ORIGIN, "http://127.0.0.1:3210");
});

test("migration verification runs SQLite integrity checks only after its copied-tree verifier succeeds", () => {
  const calls = [];
  const value = fixture({ spawnSyncImpl: (pythonPath, args, options) => { calls.push({ pythonPath, args, options }); return { status: 0 }; } });
  const manifest = [{ type: "file", path: ".rebuild-data/state.sqlite3", size: 12 }];
  assert.equal(value.coordinator.verifyRootMigrationCopy("C:\\old", "C:\\next", manifest, ["vault"], () => true), true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].pythonPath, "C:\\repo\\python-runtime\\python.exe");
  assert.equal(calls[0].args.at(-1), "C:\\next\\.rebuild-data\\state.sqlite3");
  assert.equal(calls[0].options.timeout, 30000);
  assert.equal(value.coordinator.verifyRootMigrationCopy("C:\\old", "C:\\next", manifest, ["vault"], () => false), false);
  assert.equal(calls.length, 1);
});

test("packaged bootstrap selects the formal Vault and seeds only missing settings", async () => {
  const value = fixture({ packaged: true });
  await value.coordinator.start();
  assert.deepEqual(value.events.slice(0, 5), [
    ["vault", { userDataDir: "C:\\user-data" }],
    ["vault", { userDataDir: "C:\\user-data" }],
    ["exists", "C:\\vault\\config\\settings.toml"],
    ["mkdir", "C:\\vault\\config", { recursive: true }],
    ["copy", "C:\\resources\\sidecar\\config\\settings.toml", "C:\\vault\\config\\settings.toml"],
  ]);
  assert.equal(value.supervisorOptions.moduleRoot, "C:\\resources\\sidecar");
  assert.equal(value.supervisorOptions.workingDir, "C:\\vault");
  assert.equal(value.supervisorOptions.pythonPath, "C:\\resources\\sidecar\\runtime\\python.exe");
  assert.equal(value.supervisorOptions.startupTimeoutMs, 45000);
  assert.equal(value.supervisorOptions.companionEnv.CHRIPTMAS_COMPANION_MODE, "packaged");
  assert.equal(value.supervisorOptions.companionEnv.CHRIPTMAS_COMPANION_RESOURCES_ROOT, "C:\\resources");
  assert.equal(value.recoveryOptions.workingRoot, "C:\\vault\\.rebuild-data");
});

test("packaged startup reconciles an unfinished root journal before resolving the sidecar working root", async () => {
  const oldRoot = { mode: "configured", workingDir: "C:\\old" };
  const newRoot = { mode: "configured", workingDir: "C:\\vault" };
  const value = fixture({
    packaged: true,
    vaultSequence: [oldRoot, newRoot],
    rootMigrationFactory: () => ({ async recover() { value.events.push("root-migration:recover"); return { status: "switched" }; } }),
  });
  await value.coordinator.start();
  assert.deepEqual(value.events.slice(0, 4), [
    ["vault", { userDataDir: "C:\\user-data" }],
    "root-migration:recover",
    ["vault", { userDataDir: "C:\\user-data" }],
    ["exists", "C:\\vault\\config\\settings.toml"],
  ]);
  assert.equal(value.supervisorOptions.workingDir, "C:\\vault");
});

test("root journal recovery failure blocks sidecar startup before it can write the source root", async () => {
  let recoveries = 0;
  const value = fixture({ packaged: true, rootMigrationFactory: () => ({ async recover() { recoveries += 1; throw new Error("frozen_manifest_mismatch"); } }) });
  await assert.rejects(value.coordinator.start(), /root_migration_recovery_required/);
  await assert.rejects(value.coordinator.start(), /root_migration_recovery_required/);
  assert.equal(recoveries, 2);
  assert.equal(value.events.includes("supervisor:create"), false);
  assert.equal(value.events.includes("vault:conflict"), true);
});

test("stop cancels an in-flight startup so a late sidecar start cannot revive writers", async () => {
  const events = [];
  let releaseStart;
  const delayedStart = new Promise((resolve) => { releaseStart = resolve; });
  class Supervisor {
    constructor() { this.child = null; this.session = null; events.push("create"); }
    async start() { this.child = { pid: 7 }; events.push("start"); await delayedStart; this.session = { origin: "http://127.0.0.1:7" }; return this.session; }
    async stop() { events.push("stop"); this.child = null; this.session = null; }
  }
  class Recovery { async recoverPending() {} }
  const coordinator = new DesktopRuntimeBootstrapCoordinator({
    app: { isPackaged: false, getPath: () => "C:\\user-data" }, repositoryRoot: "C:\\repo", resourcesRootProvider: () => "C:\\resources", platform: "win32", processEnv: {},
    SidecarSupervisor: Supervisor, VaultRecoveryController: Recovery, resolvePackagedVaultRoot: () => ({ mode: "configured", workingDir: "C:\\vault" }), packagedStartupTimeoutMs: 45000,
    onVaultConflict: async () => {}, onUnexpectedExit: () => {}, installRequestAuthentication: () => {}, installRendererSecurityPolicies: () => {}, startRuntimeControllers: () => {}, pathApi: path.win32,
    fsApi: { mkdirSync: () => {}, existsSync: () => true },
  });
  const starting = coordinator.start();
  await new Promise((resolve) => setImmediate(resolve));
  await coordinator.stop();
  releaseStart();
  await assert.rejects(starting, /desktop_runtime_start_cancelled/);
  assert.equal(coordinator.session, null);
  assert.deepEqual(events, ["create", "start", "stop", "stop"]);
});

test("bootstrap forwards only the resolved mixed media fixture boolean", async () => {
  const ordinary = fixture(); await ordinary.coordinator.start();
  const enabled = fixture({ e2eMediaFixture: true }); await enabled.coordinator.start();
  assert.equal(ordinary.supervisorOptions.e2eMediaFixture, false);
  assert.equal(enabled.supervisorOptions.e2eMediaFixture, true);
});

test("bootstrap forwards only the resolved plugin Hook fault boolean", async () => {
  const ordinary = fixture(); await ordinary.coordinator.start();
  const enabled = fixture({ e2ePluginHookFault: true }); await enabled.coordinator.start();
  assert.equal(ordinary.supervisorOptions.e2ePluginHookFault, false);
  assert.equal(enabled.supervisorOptions.e2ePluginHookFault, true);
});

test("bootstrap forwards only the resolved XHS controlled credential boolean", async () => {
  const ordinary = fixture(); await ordinary.coordinator.start();
  const enabled = fixture({ e2eXhsControlledCredential: true }); await enabled.coordinator.start();
  assert.equal(ordinary.supervisorOptions.e2eXhsControlledCredential, false);
  assert.equal(enabled.supervisorOptions.e2eXhsControlledCredential, true);
});

test("bootstrap forwards only the resolved XHS controlled real OCR boolean", async () => {
  const ordinary = fixture(); await ordinary.coordinator.start();
  const enabled = fixture({ e2eXhsControlledRealOcr: true }); await enabled.coordinator.start();
  assert.equal(ordinary.supervisorOptions.e2eXhsControlledRealOcr, false);
  assert.equal(enabled.supervisorOptions.e2eXhsControlledRealOcr, true);
});

test("an existing packaged settings file is preserved", async () => {
  const value = fixture({ packaged: true, settingsExist: true });
  await value.coordinator.start();
  assert.equal(value.events.some((entry) => Array.isArray(entry) && entry[0] === "copy"), false);
  assert.equal(value.events.some((entry) => Array.isArray(entry) && entry[0] === "mkdir"), false);
});

test("a conflicting packaged Vault fails closed before creating runtime owners", async () => {
  const value = fixture({ packaged: true, vault: { mode: "conflict", workingDir: null } });
  await assert.rejects(value.coordinator.start(), /vault_root_migration_required/);
  assert.deepEqual(value.events, [
    ["vault", { userDataDir: "C:\\user-data" }],
    "vault:conflict",
  ]);
  assert.equal(value.coordinator.session, null);
  assert.equal(value.coordinator.recovery, null);
});

test("stop and unexpected-exit stay narrow coordinator ports", async () => {
  const value = fixture();
  await value.coordinator.stop();
  await value.coordinator.start();
  value.supervisorOptions.onUnexpectedExit();
  await value.coordinator.stop();
  assert.deepEqual(value.events.slice(-2), ["unexpected", "supervisor:stop"]);
  assert.equal(value.recoveryOptions.sidecarOffline(), true);
});

test("constructor rejects incomplete composition dependencies", () => {
  assert.throws(() => new DesktopRuntimeBootstrapCoordinator({}), /desktop_runtime_bootstrap_options_invalid/);
});

test("an explicit Python override has priority in development and packaged mode", () => {
  for (const packaged of [false, true]) {
    const value = fixture({ packaged });
    value.processEnv.CHRIPTMAS_OS_PYTHON = "C:\\portable\\python.exe";
    assert.equal(value.coordinator.runtimePythonPath(), "C:\\portable\\python.exe");
  }
});
