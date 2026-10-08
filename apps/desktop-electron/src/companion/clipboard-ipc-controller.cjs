const fs = require("node:fs");
const path = require("node:path");

const CHANNELS = Object.freeze([
  "chriptmas:companion-clipboard-status",
  "chriptmas:companion-clipboard-enabled",
]);
const SETTINGS_FILE = "companion-clipboard.json";

class CompanionClipboardIpcController {
  constructor({
    ipcMain,
    requireMainRenderer,
    watcher,
    overlayController,
    userDataPathProvider,
    fsImpl = fs,
    processId = process.pid,
  }) {
    if (!ipcMain || typeof ipcMain.handle !== "function" || typeof ipcMain.removeHandler !== "function") {
      throw new TypeError("companion_clipboard_ipc_invalid");
    }
    if (typeof requireMainRenderer !== "function" || typeof userDataPathProvider !== "function"
      || !watcher || typeof watcher.status !== "function" || typeof watcher.setEnabled !== "function" || typeof watcher.stop !== "function"
      || !overlayController || typeof overlayController.close !== "function") {
      throw new TypeError("companion_clipboard_boundary_invalid");
    }
    if (!fsImpl || ["readFileSync", "mkdirSync", "writeFileSync", "renameSync", "unlinkSync"].some((name) => typeof fsImpl[name] !== "function")
      || !Number.isSafeInteger(processId) || processId <= 0) {
      throw new TypeError("companion_clipboard_persistence_invalid");
    }
    Object.assign(this, { ipcMain, requireMainRenderer, watcher, overlayController, userDataPathProvider, fsImpl, processId });
    this.handlers = new Map([
      [CHANNELS[0], (event) => this.status(event)],
      [CHANNELS[1], (event, payload) => this.setEnabled(event, payload)],
    ]);
    this.installed = false;
  }

  install() {
    if (this.installed) return false;
    const registered = [];
    try {
      for (const [channel, handler] of this.handlers) {
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

  initialize() {
    return this.watcher.setEnabled(this.readEnabled());
  }

  status(event) {
    this.requireMainRenderer(event);
    return this.watcher.status();
  }

  setEnabled(event, payload) {
    this.requireMainRenderer(event);
    if (!payload || typeof payload !== "object" || Array.isArray(payload)
      || Object.keys(payload).some((key) => key !== "enabled") || typeof payload.enabled !== "boolean") {
      throw new Error("companion_clipboard_setting_rejected");
    }
    this.writeEnabled(payload.enabled);
    const status = this.watcher.setEnabled(payload.enabled);
    if (!payload.enabled && this.overlayController.current?.kind?.startsWith("clipboard_")) {
      this.overlayController.close();
    }
    return status;
  }

  settingsPath() {
    return path.join(this.userDataPathProvider(), SETTINGS_FILE);
  }

  readEnabled() {
    try {
      const payload = JSON.parse(this.fsImpl.readFileSync(this.settingsPath(), "utf8"));
      return payload?.schema_version === 1 && payload?.enabled === true;
    } catch {
      return false;
    }
  }

  writeEnabled(enabled) {
    const target = this.settingsPath();
    const temporary = `${target}.${this.processId}.tmp`;
    this.fsImpl.mkdirSync(path.dirname(target), { recursive: true });
    try {
      this.fsImpl.writeFileSync(temporary, JSON.stringify({ schema_version: 1, enabled: enabled === true }), {
        encoding: "utf8",
        mode: 0o600,
      });
      this.fsImpl.renameSync(temporary, target);
    } catch (error) {
      try { this.fsImpl.unlinkSync(temporary); } catch {}
      throw error;
    }
  }

  dispose() {
    if (!this.installed) return false;
    for (const channel of CHANNELS) this.ipcMain.removeHandler(channel);
    this.watcher.stop();
    this.installed = false;
    return true;
  }
}

module.exports = { CHANNELS, CompanionClipboardIpcController, SETTINGS_FILE };
