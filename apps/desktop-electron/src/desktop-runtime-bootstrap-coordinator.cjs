const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

class DesktopRuntimeBootstrapCoordinator {
  constructor({
    app,
    repositoryRoot,
    resourcesRootProvider,
    platform,
    processEnv,
    SidecarSupervisor,
    VaultRecoveryController,
    resolvePackagedVaultRoot,
    createRootMigrationController = null,
    resolveRuntimeRootConfig = () => null,
    packagedStartupTimeoutMs,
    resolveCompanionEnv = () => ({}),
    e2eMediaFixture = false,
    e2ePluginHookFault = false,
    e2eXhsControlledCredential = false,
    e2eXhsControlledRealOcr = false,
    onVaultConflict,
    onRootMigrationRecoveryRequired = onVaultConflict,
    onUnexpectedExit,
    onRenewalFailure = () => {},
    installRequestAuthentication,
    installRendererSecurityPolicies,
    startRuntimeControllers,
    log = () => {},
    fsApi = fs,
    pathApi = path,
    spawnSyncImpl = spawnSync,
  }) {
    if (!app || typeof app.getPath !== "function" || typeof repositoryRoot !== "string" || !repositoryRoot
      || typeof resourcesRootProvider !== "function" || typeof platform !== "string" || !processEnv
      || typeof SidecarSupervisor !== "function" || typeof VaultRecoveryController !== "function"
      || typeof resolvePackagedVaultRoot !== "function" || !Number.isInteger(packagedStartupTimeoutMs)
      || typeof resolveRuntimeRootConfig !== "function"
      || typeof onVaultConflict !== "function" || typeof onUnexpectedExit !== "function"
      || typeof onRootMigrationRecoveryRequired !== "function"
      || typeof installRequestAuthentication !== "function" || typeof installRendererSecurityPolicies !== "function"
      || typeof startRuntimeControllers !== "function") {
      throw new TypeError("desktop_runtime_bootstrap_options_invalid");
    }
    this.app = app;
    this.repositoryRoot = repositoryRoot;
    this.resourcesRootProvider = resourcesRootProvider;
    this.platform = platform;
    this.processEnv = processEnv;
    this.SidecarSupervisor = SidecarSupervisor;
    this.VaultRecoveryController = VaultRecoveryController;
    this.resolvePackagedVaultRoot = resolvePackagedVaultRoot;
    this.createRootMigrationController = createRootMigrationController;
    this.resolveRuntimeRootConfig = resolveRuntimeRootConfig;
    this.packagedStartupTimeoutMs = packagedStartupTimeoutMs;
    this.resolveCompanionEnv = resolveCompanionEnv;
    this.e2eMediaFixture = e2eMediaFixture === true;
    this.e2ePluginHookFault = e2ePluginHookFault === true;
    this.e2eXhsControlledCredential = e2eXhsControlledCredential === true;
    this.e2eXhsControlledRealOcr = e2eXhsControlledRealOcr === true;
    this.onVaultConflict = onVaultConflict;
    this.onRootMigrationRecoveryRequired = onRootMigrationRecoveryRequired;
    this.onUnexpectedExit = onUnexpectedExit;
    this.onRenewalFailure = onRenewalFailure;
    this.installRequestAuthentication = installRequestAuthentication;
    this.installRendererSecurityPolicies = installRendererSecurityPolicies;
    this.startRuntimeControllers = startRuntimeControllers;
    this.log = log;
    this.fs = fsApi;
    this.path = pathApi;
    this.spawnSync = spawnSyncImpl;
    this.supervisor = null;
    this.recoveryController = null;
    this.startPromise = null;
    this.migrationPromise = null;
    this.migrationRestarting = false;
    this.lifecycleEpoch = 0;
    this.rendererBackendPort = null;
  }

  get session() {
    return this.supervisor && "usableSession" in this.supervisor
      ? this.supervisor.usableSession
      : this.supervisor?.session || null;
  }

  get recovery() {
    return this.recoveryController;
  }

  start() {
    if (this.migrationPromise !== null && !this.migrationRestarting) {
      return Promise.reject(new Error("root_migration_runtime_quiesced"));
    }
    if (this.startPromise === null) {
      const pending = this.startOnce(this.lifecycleEpoch);
      this.startPromise = pending;
      pending.catch(() => {
        if (this.startPromise === pending) this.startPromise = null;
      });
    }
    return this.startPromise;
  }

  async startOnce(startupEpoch) {
    const resourcesRoot = this.resourcesRootProvider();
    const sidecarRoot = this.app.isPackaged ? this.path.join(resourcesRoot, "sidecar") : this.repositoryRoot;
    const pythonPath = this.runtimePythonPath();
    let workingDir = this.path.join(this.repositoryRoot, "runtime");
    let runtimeRootConfig = null;
    if (!this.app.isPackaged) {
      this.fs.mkdirSync(workingDir, { recursive: true });
      this.seedPackagedSettings(this.repositoryRoot, workingDir);
    }
    if (this.app.isPackaged) {
      let vaultRoot = this.resolvePackagedVaultRoot({ userDataDir: this.app.getPath("userData") });
      if (vaultRoot.mode === "conflict" || !vaultRoot.workingDir) {
        await this.onVaultConflict();
        throw new Error("vault_root_migration_required");
      }
      await this.#recoverRootMigrationBeforeStart(startupEpoch);
      this.#assertStartupCurrent(startupEpoch);
      vaultRoot = this.resolvePackagedVaultRoot({ userDataDir: this.app.getPath("userData") });
      if (vaultRoot.mode === "conflict" || !vaultRoot.workingDir) {
        await this.onVaultConflict();
        throw new Error("vault_root_migration_required");
      }
      workingDir = vaultRoot.workingDir;
      runtimeRootConfig = this.resolveRuntimeRootConfig();
      if (!runtimeRootConfig || runtimeRootConfig.roots?.vault !== workingDir) throw new Error("sidecar_runtime_root_config_invalid");
      this.seedPackagedSettings(sidecarRoot, workingDir);
    }

    this.supervisor = new this.SidecarSupervisor({
      rootDir: this.repositoryRoot,
      moduleRoot: this.app.isPackaged ? sidecarRoot : this.path.join(this.repositoryRoot, "src"),
      workingDir,
      runtimeRootConfig,
      pythonPath,
      requestedPort: this.rendererBackendPort,
      startupTimeoutMs: this.app.isPackaged ? this.packagedStartupTimeoutMs : undefined,
      companionEnv: {
        CHRIPTMAS_COMPANION_MODE: this.app.isPackaged ? "packaged" : "development",
        CHRIPTMAS_COMPANION_USER_DATA_ROOT: this.app.getPath("userData"),
        CHRIPTMAS_COMPANION_REPOSITORY_ROOT: this.repositoryRoot,
        CHRIPTMAS_COMPANION_RESOURCES_ROOT: this.app.isPackaged ? resourcesRoot : this.repositoryRoot,
        ...this.resolveCompanionEnv(),
      },
      e2eMediaFixture: this.e2eMediaFixture,
      e2ePluginHookFault: this.e2ePluginHookFault,
      e2eXhsControlledCredential: this.e2eXhsControlledCredential,
      e2eXhsControlledRealOcr: this.e2eXhsControlledRealOcr,
      log: this.log,
      onUnexpectedExit: this.onUnexpectedExit,
      onRenewalFailure: this.onRenewalFailure,
    });
    this.recoveryController = new this.VaultRecoveryController({
      pythonPath,
      moduleRoot: this.app.isPackaged ? sidecarRoot : this.path.join(this.repositoryRoot, "src"),
      workingRoot: this.path.join(workingDir, ".rebuild-data"),
      sidecarOffline: () => !this.supervisor?.child,
    });
    await this.recoveryController.recoverPending();
    this.#assertStartupCurrent(startupEpoch);
    const active = await this.supervisor.start();
    try {
      this.#assertStartupCurrent(startupEpoch);
    } catch (error) {
      await this.supervisor.stop();
      throw error;
    }
    this.processEnv.CHRIPTMAS_DESKTOP_BACKEND_ORIGIN = active.origin;
    this.rendererBackendPort = Number(new URL(active.origin).port);
    this.installRequestAuthentication();
    this.installRendererSecurityPolicies();
    this.startRuntimeControllers();
    return active;
  }

  seedPackagedSettings(sidecarRoot, workingDir) {
    const settingsPath = this.path.join(workingDir, "config", "settings.toml");
    if (this.fs.existsSync(settingsPath)) return;
    this.fs.mkdirSync(this.path.dirname(settingsPath), { recursive: true });
    this.fs.copyFileSync(this.path.join(sidecarRoot, "config", "settings.toml"), settingsPath);
  }

  async stop() {
    this.lifecycleEpoch += 1;
    await (this.supervisor?.stop() || Promise.resolve());
    this.startPromise = null;
  }

  async #recoverRootMigrationBeforeStart(startupEpoch) {
    if (typeof this.createRootMigrationController !== "function") return;
    try {
      const controller = this.createRootMigrationController(() => {
        this.#assertStartupCurrent(startupEpoch);
        if (this.supervisor?.child) throw new Error("root_migration_sidecar_must_be_offline");
      });
      await controller.recover();
    } catch (error) {
      if (error?.message === "desktop_runtime_start_cancelled") throw error;
      await this.onRootMigrationRecoveryRequired();
      const blocked = new Error("root_migration_recovery_required");
      blocked.cause = error;
      throw blocked;
    }
  }

  #assertStartupCurrent(startupEpoch) {
    if (startupEpoch !== this.lifecycleEpoch) throw new Error("desktop_runtime_start_cancelled");
  }

  runtimePythonPath() {
    const resourcesRoot = this.resourcesRootProvider();
    const sidecarRoot = this.app.isPackaged ? this.path.join(resourcesRoot, "sidecar") : this.repositoryRoot;
    return this.processEnv.CHRIPTMAS_OS_PYTHON
      || this.path.join(sidecarRoot, this.app.isPackaged ? "runtime" : "python-runtime", this.platform === "win32" ? "python.exe" : "python");
  }

  verifyRootMigrationCopy(source, target, manifest, roles, verifyTree) {
    if (typeof verifyTree !== "function" || !verifyTree(source, target, manifest, roles)) return false;
    const databases = Array.isArray(manifest)
      ? manifest.filter((entry) => entry?.type === "file" && /(?:\.sqlite|\.sqlite3|\.db)$/i.test(entry.path))
        .map((entry) => this.path.join(target, entry.path))
      : [];
    if (databases.length === 0) return true;
    const integrityScript = [
      "import sqlite3, sys",
      "from pathlib import Path",
      "for candidate in sys.argv[1:] :",
      "    connection = sqlite3.connect(Path(candidate).resolve().as_uri() + '?mode=ro', uri=True)",
      "    try:",
      "        result = connection.execute('PRAGMA integrity_check').fetchone()",
      "        if result != ('ok',): raise RuntimeError('sqlite_integrity_check_failed')",
      "    finally:",
      "        connection.close()",
    ].join("\n");
    const result = this.spawnSync(this.runtimePythonPath(), ["-c", integrityScript, ...databases], {
      cwd: target,
      windowsHide: true,
      encoding: "utf8",
      timeout: 30000,
    });
    return result?.status === 0 && !result.error;
  }

  /**
   * Runs an operation while the only sidecar writer is demonstrably stopped.
   * The caller receives a probe instead of a boolean so it cannot accidentally
   * bless a SQLite tree without checking the live supervisor state.
   */
  runRootMigrationExclusive(work, { restartOnFailure = false } = {}) {
    if (typeof work !== "function") return Promise.reject(new TypeError("root_migration_work_invalid"));
    if (typeof restartOnFailure !== "boolean") return Promise.reject(new TypeError("root_migration_restart_policy_invalid"));
    if (this.migrationPromise !== null) return Promise.reject(new Error("root_migration_already_running"));
    this.migrationPromise = this.#runRootMigrationExclusive(work, restartOnFailure);
    return this.migrationPromise.finally(() => { this.migrationPromise = null; });
  }

  async #runRootMigrationExclusive(work, restartOnFailure) {
    await this.stop();
    if (this.supervisor?.child) throw new Error("root_migration_sidecar_still_online");
    const assertSourceQuiescent = () => {
      if (this.supervisor?.child) throw new Error("root_migration_sidecar_must_be_offline");
    };
    let value;
    let operationError = null;
    try {
      value = await work(Object.freeze({ assertSourceQuiescent }));
    } catch (error) {
      operationError = error;
    }
    if (operationError && !restartOnFailure) throw operationError;
    this.migrationRestarting = true;
    try {
      const session = await this.start();
      if (operationError) throw operationError;
      return Object.freeze({ value, session });
    } catch (restartError) {
      if (operationError) {
        throw operationError;
      }
      throw restartError;
    } finally {
      this.migrationRestarting = false;
    }
  }
}

module.exports = { DesktopRuntimeBootstrapCoordinator };
