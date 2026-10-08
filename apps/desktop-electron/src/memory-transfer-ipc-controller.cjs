const { currentSessionForRequest } = require("./desktop-session.cjs");
const fs = require("node:fs");
const path = require("node:path");
const { SESSION_HEADER } = require("./sidecar-supervisor.cjs");
const { MAX_PACKAGE_BYTES, writeMemoryAssetPackage } = require("./memory-asset-export.cjs");
const {
  MAX_EXPORT_BYTES,
  validateMemoryExportRequest,
  writeMemoryPresetExport,
} = require("./memory-preset-export.cjs");

const CHANNELS = Object.freeze([
  "chriptmas:memory-assets-export",
  "chriptmas:memory-assets-import",
  "chriptmas:memory-export-save",
]);

class MemoryTransferIpcController {
  constructor({
    ipcMain,
    dialog,
    requireMainRenderer,
    mainWindowProvider,
    sessionProvider,
    fetchImpl = fetch,
    timeoutSignal = (milliseconds) => AbortSignal.timeout(milliseconds),
    fsPromises = fs.promises,
    pathApi = path,
    writeMemoryAssetPackage: writeAssetPackage = writeMemoryAssetPackage,
    validateMemoryExportRequest: validateExportRequest = validateMemoryExportRequest,
    writeMemoryPresetExport: writePresetExport = writeMemoryPresetExport,
    maxPackageBytes = MAX_PACKAGE_BYTES,
    maxExportBytes = MAX_EXPORT_BYTES,
    now = () => new Date(),
  }) {
    if (!ipcMain || typeof ipcMain.handle !== "function" || typeof ipcMain.removeHandler !== "function") {
      throw new TypeError("memory_transfer_ipc_invalid");
    }
    if (!dialog || typeof dialog.showSaveDialog !== "function" || typeof dialog.showOpenDialog !== "function") {
      throw new TypeError("memory_transfer_dialog_invalid");
    }
    if (typeof requireMainRenderer !== "function" || typeof mainWindowProvider !== "function"
      || typeof sessionProvider !== "function" || typeof fetchImpl !== "function" || typeof timeoutSignal !== "function") {
      throw new TypeError("memory_transfer_boundary_invalid");
    }
    if (!fsPromises || typeof fsPromises.lstat !== "function" || typeof fsPromises.readFile !== "function"
      || !pathApi || typeof pathApi.extname !== "function" || typeof pathApi.basename !== "function") {
      throw new TypeError("memory_transfer_file_runtime_invalid");
    }
    if (typeof writeAssetPackage !== "function" || typeof validateExportRequest !== "function"
      || typeof writePresetExport !== "function" || typeof now !== "function") {
      throw new TypeError("memory_transfer_contract_invalid");
    }
    this.ipcMain = ipcMain;
    this.dialog = dialog;
    this.requireMainRenderer = requireMainRenderer;
    this.mainWindowProvider = mainWindowProvider;
    this.sessionProvider = sessionProvider;
    this.fetch = fetchImpl;
    this.timeoutSignal = timeoutSignal;
    this.fsPromises = fsPromises;
    this.path = pathApi;
    this.writeMemoryAssetPackage = writeAssetPackage;
    this.validateMemoryExportRequest = validateExportRequest;
    this.writeMemoryPresetExport = writePresetExport;
    this.maxPackageBytes = maxPackageBytes;
    this.maxExportBytes = maxExportBytes;
    this.now = now;
    this.installed = false;
  }

  install() {
    if (this.installed) return false;
    const handlers = new Map([
      ["chriptmas:memory-assets-export", (event) => this.exportMemoryAssets(event)],
      ["chriptmas:memory-assets-import", (event) => this.importMemoryAssets(event)],
      ["chriptmas:memory-export-save", (event, payload) => this.saveMemoryExport(event, payload)],
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

  requireWindow(errorCode) {
    const mainWindow = this.mainWindowProvider();
    if (!mainWindow?.isVisible() || !mainWindow?.isFocused()) throw new Error(errorCode);
    return mainWindow;
  }

  requireSession(errorCode) {
    const session = this.sessionProvider();
    if (!session) throw new Error(errorCode);
    return session;
  }

  async exportMemoryAssets(event) {
    this.requireMainRenderer(event);
    const mainWindow = this.requireWindow("memory_asset_export_window_required");
    const selected = await this.dialog.showSaveDialog(mainWindow, {
      title: "备份记忆资产包",
      defaultPath: `Chriptmas-memory-assets-${this.now().toISOString().slice(0, 10)}.json`,
      filters: [{ name: "Chriptmas 记忆资产包", extensions: ["json"] }],
      properties: ["createDirectory", "showOverwriteConfirmation"],
    });
    if (selected.canceled || !selected.filePath) return { status: "cancelled" };
    const session = this.requireSession("memory_asset_export_sidecar_unavailable");
    const response = await this.fetch(`${session.origin}/api/rebuild/memory-assets/export`, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        [SESSION_HEADER]: session.secret,
      },
      body: "{}",
      signal: this.timeoutSignal(120000),
    });
    if (!response.ok) throw new Error(`memory_asset_export_http_${response.status}`);
    const declaredLength = Number(response.headers.get("content-length") || 0);
    if (declaredLength > this.maxPackageBytes) throw new Error("memory_asset_export_too_large");
    const bytes = Buffer.from(await response.arrayBuffer());
    if (bytes.byteLength > this.maxPackageBytes) throw new Error("memory_asset_export_too_large");
    let packagePayload;
    try {
      packagePayload = JSON.parse(bytes.toString("utf8"));
    } catch {
      throw new Error("memory_asset_export_payload_invalid");
    }
    return this.writeMemoryAssetPackage({ targetPath: selected.filePath, packagePayload });
  }

  async importMemoryAssets(event) {
    this.requireMainRenderer(event);
    const mainWindow = this.requireWindow("memory_asset_import_window_required");
    const selected = await this.dialog.showOpenDialog(mainWindow, {
      title: "导入记忆资产包",
      filters: [{ name: "Chriptmas 记忆资产包", extensions: ["zip"] }],
      properties: ["openFile", "dontAddToRecent"],
    });
    if (selected.canceled || selected.filePaths.length !== 1) return { status: "cancelled" };
    const sourcePath = selected.filePaths[0];
    if (this.path.extname(sourcePath).toLowerCase() !== ".zip") throw new Error("memory_asset_import_type_invalid");
    const sourceStat = await this.fsPromises.lstat(sourcePath);
    if (!sourceStat.isFile() || sourceStat.isSymbolicLink()) throw new Error("memory_asset_import_source_invalid");
    if (sourceStat.size < 1 || sourceStat.size > this.maxPackageBytes) throw new Error("memory_asset_import_size_invalid");
    const startingSession = { ...this.requireSession("memory_asset_import_sidecar_unavailable") };
    const zipBytes = await this.fsPromises.readFile(sourcePath);
    if (zipBytes.byteLength < 1 || zipBytes.byteLength > this.maxPackageBytes) {
      throw new Error("memory_asset_import_size_changed");
    }
    const session = currentSessionForRequest(this.sessionProvider, startingSession, "memory_asset_import_sidecar_unavailable");
    const response = await this.fetch(`${session.origin}/api/rebuild/memory/export/round-trip`, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        [SESSION_HEADER]: session.secret,
      },
      body: JSON.stringify({ zip_base64: zipBytes.toString("base64") }),
      signal: this.timeoutSignal(120000),
    });
    const receipt = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = typeof receipt?.reason === "string"
        ? receipt.reason
        : typeof receipt?.detail === "string"
          ? receipt.detail
          : typeof receipt?.error === "string"
            ? receipt.error
            : `HTTP ${response.status}`;
      throw new Error(`memory_asset_import_rejected:${detail}`);
    }
    return Object.freeze({ status: "imported", file_name: this.path.basename(sourcePath), result: receipt });
  }

  async saveMemoryExport(event, payload) {
    this.requireMainRenderer(event);
    const mainWindow = this.requireWindow("memory_export_window_required");
    const request = this.validateMemoryExportRequest(payload);
    const extension = request.config.extension.slice(1);
    const selected = await this.dialog.showSaveDialog(mainWindow, {
      title: "导出记忆",
      defaultPath: `Chriptmas-memory-export-${this.now().toISOString().slice(0, 10)}${request.config.extension}`,
      filters: [{ name: request.config.label, extensions: [extension] }],
      properties: ["createDirectory", "showOverwriteConfirmation"],
    });
    if (selected.canceled || !selected.filePath) return { status: "cancelled" };
    const session = this.requireSession("memory_export_sidecar_unavailable");
    const response = await this.fetch(`${session.origin}/api/rebuild/memory/export/file`, {
      method: "POST",
      headers: {
        Accept: "application/octet-stream",
        "Content-Type": "application/json",
        [SESSION_HEADER]: session.secret,
      },
      body: JSON.stringify({ preset: request.preset, scope: request.scope }),
      signal: this.timeoutSignal(120000),
    });
    if (!response.ok) throw new Error(`memory_export_http_${response.status}`);
    const declaredLength = Number(response.headers.get("content-length") || 0);
    if (declaredLength > this.maxExportBytes) throw new Error("memory_export_too_large");
    const bytes = Buffer.from(await response.arrayBuffer());
    if (bytes.byteLength > this.maxExportBytes) throw new Error("memory_export_too_large");
    return this.writeMemoryPresetExport({
      targetPath: selected.filePath,
      bytes,
      preset: request.preset,
      responseFormat: response.headers.get("x-chriptmas-export-format") || "",
    });
  }

  dispose() {
    if (!this.installed) return false;
    for (const channel of CHANNELS) this.ipcMain.removeHandler(channel);
    this.installed = false;
    return true;
  }
}

module.exports = { CHANNELS, MemoryTransferIpcController };
