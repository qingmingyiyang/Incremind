const CHANNELS = Object.freeze([
  "chriptmas:companion-easter-egg-status",
  "chriptmas:companion-easter-egg-enabled",
  "chriptmas:companion-local-action",
]);

class CompanionEasterEggIpcController {
  constructor({ ipcMain, requireMainRenderer, runtime }) {
    if (!ipcMain || typeof ipcMain.handle !== "function" || typeof ipcMain.removeHandler !== "function") {
      throw new TypeError("companion_easter_egg_ipc_invalid");
    }
    if (typeof requireMainRenderer !== "function" || !runtime
      || ![runtime.status, runtime.setEnabled, runtime.record].every((method) => typeof method === "function")) {
      throw new TypeError("companion_easter_egg_boundary_invalid");
    }
    this.ipcMain = ipcMain;
    this.requireMainRenderer = requireMainRenderer;
    this.runtime = runtime;
    this.installed = false;
  }

  install() {
    if (this.installed) return false;
    const handlers = new Map([
      ["chriptmas:companion-easter-egg-status", (event) => this.status(event)],
      ["chriptmas:companion-easter-egg-enabled", (event, payload) => this.setEnabled(event, payload)],
      ["chriptmas:companion-local-action", (event, payload) => this.recordLocalAction(event, payload)],
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

  status(event) {
    this.requireMainRenderer(event);
    return this.runtime.status();
  }

  setEnabled(event, payload) {
    this.requireMainRenderer(event);
    if (!payload || typeof payload !== "object" || Array.isArray(payload)
      || Object.keys(payload).join() !== "enabled" || typeof payload.enabled !== "boolean") {
      throw new Error("companion_easter_egg_setting_rejected");
    }
    return this.runtime.setEnabled(payload.enabled);
  }

  recordLocalAction(event, payload) {
    this.requireMainRenderer(event);
    if (!payload || typeof payload !== "object" || Array.isArray(payload)
      || Object.keys(payload).join() !== "action" || payload.action !== "minigame_play") {
      throw new Error("companion_local_action_rejected");
    }
    return this.runtime.record("minigame.play");
  }

  dispose() {
    if (!this.installed) return false;
    for (const channel of CHANNELS) this.ipcMain.removeHandler(channel);
    this.installed = false;
    return true;
  }
}

module.exports = { CHANNELS, CompanionEasterEggIpcController };
