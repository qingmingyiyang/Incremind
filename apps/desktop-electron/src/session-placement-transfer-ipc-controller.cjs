const fs = require("node:fs");
const path = require("node:path");
const { SESSION_HEADER } = require("./sidecar-supervisor.cjs");
const CHANNELS = Object.freeze([
  "chriptmas:session-placement-save",
  "chriptmas:session-placement-import",
  "chriptmas:session-placement-reconcile",
]);
const MAX_BUNDLE_BYTES = 262_144;
const DESKTOP_CONTROL_HEADER = "X-Chriptmas-Session-Placement-Control";
const BUNDLE_COMMON_FIELDS = new Set(["schema_version", "bundle_id", "source_device_id", "target_device_id", "project_id", "session_ref", "turn_refs", "context_manifest_ref", "context_manifest_revision", "last_event_cursor", "display_summary", "workspace_base_manifest_ref", "workspace_manifest", "capability_descriptors", "expires_at", "nonce", "signature"]);
const BUNDLE_V3_TRUST_FIELDS = new Set(["source_trust_revision", "target_trust_revision"]);
const RECONCILE_CLASSIFICATIONS = Object.freeze(["unchanged", "add", "modify", "delete", "conflict"]);

class SessionPlacementTransferIpcController {
  constructor({
    ipcMain,
    dialog,
    requireMainRenderer,
    mainWindowProvider,
    sessionProvider,
    fetchImpl = fetch,
    fsPromises = fs.promises,
  }) {
    if (!ipcMain?.handle || !ipcMain?.removeHandler || !dialog?.showSaveDialog || !dialog?.showOpenDialog) {
      throw new TypeError("session_placement_ipc_invalid");
    }
    if (typeof requireMainRenderer !== "function" || typeof mainWindowProvider !== "function"
      || typeof sessionProvider !== "function" || typeof fetchImpl !== "function") {
      throw new TypeError("session_placement_boundary_invalid");
    }
    if (!fsPromises?.writeFile || !fsPromises?.lstat || !fsPromises?.readFile) {
      throw new TypeError("session_placement_file_runtime_invalid");
    }
    this.ipcMain = ipcMain;
    this.dialog = dialog;
    this.requireMainRenderer = requireMainRenderer;
    this.mainWindowProvider = mainWindowProvider;
    this.sessionProvider = sessionProvider;
    this.fetch = fetchImpl;
    this.fs = fsPromises;
    this.installed = false;
  }

  install() {
    if (this.installed) return false;
    const handlers = new Map([
      [CHANNELS[0], (event, dto) => this.save(event, dto)],
      [CHANNELS[1], (event) => this.import(event)],
      [CHANNELS[2], (event, dto) => this.reconcile(event, dto)],
    ]);
    const registered = [];
    try {
      for (const [channel, handler] of handlers) {
        this.ipcMain.handle(channel, handler);
        registered.push(channel);
      }
    } catch (error) {
      for (const channel of registered) this.ipcMain.removeHandler(channel);
      throw error;
    }
    this.installed = true;
    return true;
  }

  _window() {
    const win = this.mainWindowProvider();
    if (!win || win.isDestroyed?.() || !win.isVisible?.() || !win.isFocused?.()) {
      throw new Error("session_bundle_window_required");
    }
    return win;
  }

  _session() {
    const session = this.sessionProvider();
    if (!session || !/^http:\/\/127\.0\.0\.1:\d+$/.test(session.origin || "") || !session.secret) {
      throw new Error("session_bundle_sidecar_unavailable");
    }
    return session;
  }
  async save(event, value) {
    this.requireMainRenderer(event); const dto = exportDto(value); const win = this._window();
    const workspace = await this.dialog.showOpenDialog(win, {
      title: "选择会话工作区", properties: ["openDirectory", "dontAddToRecent"],
    });
    if (workspace.canceled || !Array.isArray(workspace.filePaths) || workspace.filePaths.length !== 1
      || typeof workspace.filePaths[0] !== "string" || !workspace.filePaths[0]) return { status: "cancelled" };
    const pick = await this.dialog.showSaveDialog(win, { title: "保存会话恢复包", defaultPath: "Chriptmas-session-resume.json", filters: [{ name: "Chriptmas 会话恢复包", extensions: ["json"] }], properties: ["createDirectory", "showOverwriteConfirmation"] });
    if (pick.canceled || !pick.filePath) return { status: "cancelled" };
    if (path.extname(pick.filePath).toLowerCase() !== ".json") throw new Error("session_bundle_target_invalid");
    const session = this._session(); const response = await this.fetch(`${session.origin}/api/rebuild/session-placement/exports`, { method: "POST", credentials: "omit", cache: "no-store", headers: { Accept: "application/json", "Content-Type": "application/json", [SESSION_HEADER]: session.secret, [DESKTOP_CONTROL_HEADER]: "main-v1" }, body: JSON.stringify({ ...dto, workspace_path: workspace.filePaths[0] }) });
    if (!response.ok) throw new Error("session_bundle_export_rejected");
    const declaredLength = Number(response.headers?.get?.("content-length") || 0);
    if (declaredLength > MAX_BUNDLE_BYTES) throw new Error("session_bundle_too_large");
    const bundle = bundleDto(await response.json().catch(() => null)); const bytes = Buffer.from(JSON.stringify(bundle), "utf8"); if (bytes.byteLength > MAX_BUNDLE_BYTES) throw new Error("session_bundle_too_large");
    try { await this.fs.writeFile(pick.filePath, bytes, { flag: "wx" }); } catch (error) { throw new Error(error?.code === "EEXIST" ? "session_bundle_target_exists" : "session_bundle_write_failed"); }
    return { status: "saved", bundle_id: bundle.bundle_id };
  }
  async import(event) {
    this.requireMainRenderer(event); const win = this._window(); const pick = await this.dialog.showOpenDialog(win, { title: "导入会话恢复包", filters: [{ name: "Chriptmas 会话恢复包", extensions: ["json"] }], properties: ["openFile", "dontAddToRecent"] });
    if (pick.canceled || !Array.isArray(pick.filePaths) || pick.filePaths.length !== 1) return { status: "cancelled" };
    const sourcePath = pick.filePaths[0]; const stat = await this.fs.lstat(sourcePath);
    if (path.extname(sourcePath).toLowerCase() !== ".json" || !stat.isFile() || stat.isSymbolicLink() || stat.size < 1 || stat.size > MAX_BUNDLE_BYTES) throw new Error("session_bundle_invalid");
    let bundle; try { bundle = bundleDto(JSON.parse(await this.fs.readFile(sourcePath, "utf8"))); } catch { throw new Error("session_bundle_invalid"); }
    const session = this._session(); const response = await this.fetch(`${session.origin}/api/rebuild/session-placement/imports`, { method: "POST", credentials: "omit", cache: "no-store", headers: { Accept: "application/json", "Content-Type": "application/json", [SESSION_HEADER]: session.secret }, body: JSON.stringify({ bundle }) });
    if (!response.ok) throw new Error("session_bundle_import_rejected"); const projection = await response.json().catch(() => null); if (!projection || projection.read_only !== true || typeof projection.bundle_id !== "string") throw new Error("session_bundle_import_invalid"); return { status: "imported", bundle_id: projection.bundle_id, read_only: true };
  }
  async reconcile(event, value) {
    this.requireMainRenderer(event); const dto = reconcileDto(value); const win = this._window();
    const workspace = await this.dialog.showOpenDialog(win, {
      title: "选择待对账工作区", properties: ["openDirectory", "dontAddToRecent"],
    });
    if (workspace.canceled || !Array.isArray(workspace.filePaths) || workspace.filePaths.length !== 1
      || typeof workspace.filePaths[0] !== "string" || !workspace.filePaths[0]) return { status: "cancelled" };
    const session = this._session(); const response = await this.fetch(`${session.origin}/api/rebuild/session-placement/recoveries/${encodeURIComponent(dto.bundle_id)}/reconcile-plan`, { method: "POST", credentials: "omit", cache: "no-store", headers: { Accept: "application/json", "Content-Type": "application/json", [SESSION_HEADER]: session.secret, [DESKTOP_CONTROL_HEADER]: "main-v1" }, body: JSON.stringify({ project_id: dto.project_id, workspace_path: workspace.filePaths[0] }) });
    if (!response.ok) throw new Error("session_bundle_reconcile_rejected");
    return reconcileResultDto(await response.json().catch(() => null));
  }
  dispose() { if (!this.installed) return false; for (const channel of CHANNELS) this.ipcMain.removeHandler(channel); this.installed = false; return true; }
}
function exportDto(value) { if (!value || typeof value !== "object" || Object.keys(value).sort().join(",") !== "expires_at,project_id,session_id,target_device_id") throw new Error("session_bundle_request_invalid"); for (const key of ["project_id", "session_id", "target_device_id", "expires_at"]) if (typeof value[key] !== "string" || !value[key].trim() || value[key].length > 256) throw new Error("session_bundle_request_invalid"); return { project_id: value.project_id, session_id: value.session_id, target_device_id: value.target_device_id, expires_at: value.expires_at }; }
function bundleDto(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("session_bundle_invalid");
  }
  const isV3 = value.schema_version === "resume_bundle.v3";
  const allowed = isV3 ? new Set([...BUNDLE_COMMON_FIELDS, ...BUNDLE_V3_TRUST_FIELDS]) : BUNDLE_COMMON_FIELDS;
  if ((isV3 ? value.schema_version !== "resume_bundle.v3" : value.schema_version !== "resume_bundle.v2")
    || Object.keys(value).length !== allowed.size || Object.keys(value).some((key) => !allowed.has(key))) throw new Error("session_bundle_invalid");
  for (const key of [
    "schema_version", "bundle_id", "source_device_id", "target_device_id", "project_id", "session_ref",
    "context_manifest_ref", "context_manifest_revision", "last_event_cursor", "display_summary",
    "workspace_base_manifest_ref", "expires_at", "nonce", "signature",
  ]) {
    if (typeof value[key] !== "string" || !value[key] || value[key].length > 2048) {
      throw new Error("session_bundle_invalid");
    }
  }
  if (!Array.isArray(value.turn_refs) || value.turn_refs.length > 64
    || value.turn_refs.some((item) => typeof item !== "string" || !item || item.length > 256)
    || !Array.isArray(value.workspace_manifest) || value.workspace_manifest.length > 512
    || !Array.isArray(value.capability_descriptors) || value.capability_descriptors.length > 64) {
    throw new Error("session_bundle_invalid");
  }
  if (isV3 && [...BUNDLE_V3_TRUST_FIELDS].some((key) => !Number.isSafeInteger(value[key]) || value[key] < 1)) throw new Error("session_bundle_invalid");
  return value;
}
function reconcileDto(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)
    || Object.keys(value).sort().join(",") !== "bundle_id,project_id") throw new Error("session_bundle_reconcile_invalid");
  for (const key of ["bundle_id", "project_id"]) if (typeof value[key] !== "string" || !value[key].trim() || value[key].length > 128) throw new Error("session_bundle_reconcile_invalid");
  return { bundle_id: value.bundle_id, project_id: value.project_id };
}
function reconcileResultDto(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)
    || typeof value.has_conflicts !== "boolean" || value.apply_supported !== false) throw new Error("session_bundle_reconcile_invalid");
  const counts = {};
  if (value.counts && typeof value.counts === "object" && !Array.isArray(value.counts)
    && Object.keys(value.counts).length === RECONCILE_CLASSIFICATIONS.length
    && RECONCILE_CLASSIFICATIONS.every((key) => Number.isSafeInteger(value.counts[key]) && value.counts[key] >= 0 && value.counts[key] <= 512)) {
    for (const key of RECONCILE_CLASSIFICATIONS) counts[key] = value.counts[key];
  } else if (value.classifications && typeof value.classifications === "object" && !Array.isArray(value.classifications)
    && Object.keys(value.classifications).length === RECONCILE_CLASSIFICATIONS.length
    && RECONCILE_CLASSIFICATIONS.every((key) => Array.isArray(value.classifications[key]) && value.classifications[key].length <= 512)) {
    for (const key of RECONCILE_CLASSIFICATIONS) counts[key] = value.classifications[key].length;
  } else throw new Error("session_bundle_reconcile_invalid");
  return { status: "reconciled", counts, has_conflicts: value.has_conflicts, apply_supported: false };
}
module.exports = { CHANNELS, SessionPlacementTransferIpcController };
