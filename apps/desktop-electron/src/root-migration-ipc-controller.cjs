const CHANNELS = Object.freeze({
  inspect: "chriptmas:root-migration-inspect",
  select: "chriptmas:root-migration-select",
  preflight: "chriptmas:root-migration-preflight",
  execute: "chriptmas:root-migration-execute",
  recover: "chriptmas:root-migration-recover",
});

const ROOT_KEYS = Object.freeze(["vaultRoot", "modelRoot", "mediaRoot"]);
const ROLE_TO_KEY = Object.freeze({ vault: "vaultRoot", model: "modelRoot", media: "mediaRoot" });
const PREPARE_TTL_MS = 5 * 60 * 1000;
const DEFAULT_TARGET_PREFIX = "Chriptmas-data";

function isRootTarget(value) {
  return Boolean(value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).sort().join(",") === ROOT_KEYS.slice().sort().join(",")
    && ROOT_KEYS.every((key) => typeof value[key] === "string" && value[key].trim().length > 0
      && value[key].length <= 1024 && !value[key].includes("\0")));
}

function isExecuteRequest(value) {
  return Boolean(value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).sort().join(",") === "operationId,target"
    && typeof value.operationId === "string" && /^root-[a-z0-9-]{12,120}$/i.test(value.operationId)
    && isRootTarget(value.target));
}

function isSupportedTarget(value) {
  return isRootTarget(value) && value.vaultRoot === value.modelRoot && value.vaultRoot === value.mediaRoot;
}

function sanitizeDiagnostic(value) {
  const configured = normalizeConfiguredRoots(value?.configured);
  const actual = value?.actual;
  return Object.freeze({
    mode: typeof value?.mode === "string" ? value.mode : "unavailable",
    configured: isRootTarget(configured) ? Object.freeze({ ...configured }) : null,
    actual: actual && typeof actual === "object" ? Object.freeze(Object.fromEntries(
      Object.entries(actual).filter(([role, item]) => Object.hasOwn(ROLE_TO_KEY, role)).map(([role, item]) => [role, Object.freeze({
        available: item?.exists === true,
      })]),
    )) : null,
  });
}

class RootMigrationIpcController {
  constructor({ ipcMain, dialog, requireMainRenderer, mainWindowProvider, controllerFactory, runExclusive, runtimeAvailable = () => true, now = () => Date.now(), pathApi = require("node:path"), exists = require("node:fs").existsSync, idFactory = require("node:crypto").randomUUID }) {
    if (!ipcMain || typeof ipcMain.handle !== "function" || typeof ipcMain.removeHandler !== "function") throw new TypeError("root_migration_ipc_invalid");
    if (!dialog || typeof dialog.showOpenDialog !== "function" || typeof dialog.showMessageBox !== "function") throw new TypeError("root_migration_dialog_invalid");
    if ([requireMainRenderer, mainWindowProvider, controllerFactory, runExclusive, runtimeAvailable, now, exists, idFactory].some((value) => typeof value !== "function") || !pathApi || typeof pathApi.join !== "function") throw new TypeError("root_migration_boundary_invalid");
    this.ipcMain = ipcMain;
    this.dialog = dialog;
    this.requireMainRenderer = requireMainRenderer;
    this.mainWindowProvider = mainWindowProvider;
    this.controllerFactory = controllerFactory;
    this.runExclusive = runExclusive;
    this.runtimeAvailable = runtimeAvailable;
    this.now = now;
    this.path = pathApi;
    this.exists = exists;
    this.idFactory = idFactory;
    this.prepared = null;
    this.installed = false;
  }

  install() {
    if (this.installed) return false;
    this.ipcMain.handle(CHANNELS.inspect, (event) => this.inspect(event));
    this.ipcMain.handle(CHANNELS.select, (event, payload) => this.select(event, payload));
    this.ipcMain.handle(CHANNELS.preflight, (event, target) => this.preflight(event, target));
    this.ipcMain.handle(CHANNELS.execute, (event, request) => this.execute(event, request));
    this.ipcMain.handle(CHANNELS.recover, (event) => this.recover(event));
    this.installed = true;
    return true;
  }

  inspect(event) {
    this.requireMainRenderer(event);
    if (!this.runtimeAvailable()) return Object.freeze({ mode: "unavailable", configured: null, actual: null });
    return sanitizeDiagnostic(this.controllerFactory(() => {}).diagnose());
  }

  async select(event, payload) {
    this.requireMainRenderer(event);
    this.#requireRuntime();
    const role = typeof payload?.role === "string" ? payload.role : "";
    if (!Object.hasOwn(ROLE_TO_KEY, role)) throw new Error("root_migration_role_invalid");
    const window = this.#requireFocusedWindow();
    const selection = await this.dialog.showOpenDialog(window, {
      title: "选择新数据根目录的上级文件夹",
      properties: ["openDirectory", "createDirectory"],
    });
    if (selection.canceled || selection.filePaths.length !== 1) return Object.freeze({ status: "cancelled" });
    const target = this.#newTargetWithin(selection.filePaths[0]);
    return Object.freeze({ status: "selected", role, path: target });
  }

  async preflight(event, target) {
    this.requireMainRenderer(event);
    this.#requireRuntime();
    this.#requireFocusedWindow();
    if (!isSupportedTarget(target)) throw new Error("root_migration_independent_root_layout_unsupported");
    const outcome = await this.runExclusive(async ({ assertSourceQuiescent }) => {
      const controller = this.controllerFactory(assertSourceQuiescent);
      return controller.preflight({ target });
    }, { restartOnFailure: true });
    const summary = outcome.value;
    this.prepared = Object.freeze({ operationId: summary.operationId, target: Object.freeze({ ...target }), expiresAt: this.now() + PREPARE_TTL_MS });
    return Object.freeze({ ...summary, restart_status: "ready", preflight_mode: "sidecar_quiesced" });
  }

  async execute(event, request) {
    this.requireMainRenderer(event);
    this.#requireRuntime();
    const window = this.#requireFocusedWindow();
    if (!isExecuteRequest(request) || !isSupportedTarget(request.target)) throw new Error("root_migration_execute_request_invalid");
    if (!this.#matchesPrepared(request)) throw new Error("root_migration_preflight_expired");
    const confirmation = await this.dialog.showMessageBox(window, {
      type: "warning",
      title: "确认迁移本地数据根目录",
      message: "应用会暂时停止本地处理，复制并校验资料后切换根目录。",
      detail: "旧数据不会被删除。若复制、校验或重启失败，应用会保留迁移记录和旧副本，以便重新打开后继续核对。",
      buttons: ["取消", "确认迁移"],
      defaultId: 0,
      cancelId: 0,
      noLink: true,
    });
    if (confirmation.response !== 1) return Object.freeze({ status: "cancelled" });
    const outcome = await this.runExclusive(async ({ assertSourceQuiescent }) => {
      const controller = this.controllerFactory(assertSourceQuiescent);
      return controller.execute({ target: request.target });
    }, { restartOnFailure: false });
    const observed = sanitizeDiagnostic(this.controllerFactory(() => {}).diagnose());
    if (!sameTarget(observed.configured, request.target)) throw new Error("root_migration_restart_root_mismatch");
    this.prepared = null;
    return Object.freeze({ ...outcome.value, observed, restart_status: "ready" });
  }

  async recover(event) {
    this.requireMainRenderer(event);
    this.#requireRuntime();
    this.#requireFocusedWindow();
    const outcome = await this.runExclusive(async ({ assertSourceQuiescent }) => this.controllerFactory(assertSourceQuiescent).recover(), { restartOnFailure: false });
    return Object.freeze({ ...outcome.value, observed: sanitizeDiagnostic(this.controllerFactory(() => {}).diagnose()), restart_status: "ready" });
  }

  dispose() {
    if (!this.installed) return false;
    Object.values(CHANNELS).forEach((channel) => this.ipcMain.removeHandler(channel));
    this.prepared = null;
    this.installed = false;
    return true;
  }

  #requireFocusedWindow() {
    const window = this.mainWindowProvider();
    if (!window?.isVisible?.() || !window?.isFocused?.()) throw new Error("root_migration_window_required");
    return window;
  }

  #requireRuntime() {
    if (!this.runtimeAvailable()) throw new Error("root_migration_packaged_runtime_required");
  }

  #newTargetWithin(parent) {
    for (let attempt = 0; attempt < 8; attempt += 1) {
      const suffix = String(this.idFactory()).replace(/[^a-z0-9]/gi, "").slice(0, 12);
      const target = this.path.join(parent, `${DEFAULT_TARGET_PREFIX}-${this.now()}-${suffix}`);
      if (!this.exists(target)) return target;
    }
    throw new Error("root_migration_target_name_collision");
  }

  #matchesPrepared(request) {
    return this.prepared !== null && this.prepared.operationId === request.operationId
      && this.prepared.expiresAt >= this.now() && sameTarget(this.prepared.target, request.target);
  }
}

function sameTarget(left, right) {
  return isRootTarget(left) && isRootTarget(right) && ROOT_KEYS.every((key) => left[key] === right[key]);
}

function normalizeConfiguredRoots(value) {
  if (isRootTarget(value)) return Object.freeze({ ...value });
  const vaultRoot = typeof value?.root === "string" ? value.root : "";
  const modelRoot = typeof value?.roots?.model === "string" ? value.roots.model : "";
  const mediaRoot = typeof value?.roots?.media === "string" ? value.roots.media : "";
  return isRootTarget({ vaultRoot, modelRoot, mediaRoot }) ? Object.freeze({ vaultRoot, modelRoot, mediaRoot }) : null;
}

module.exports = { CHANNELS, DEFAULT_TARGET_PREFIX, PREPARE_TTL_MS, RootMigrationIpcController, isExecuteRequest, isRootTarget, isSupportedTarget, sanitizeDiagnostic };
