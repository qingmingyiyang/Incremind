const CHANNELS = Object.freeze([
  "chriptmas:companion-vision-sources",
  "chriptmas:companion-vision-capture",
  "chriptmas:companion-vision-confirm",
  "chriptmas:companion-vision-cancel",
]);

class CompanionScreenVisionIpcController {
  constructor({ ipcMain, requireMainRenderer, visionProvider }) {
    if (!ipcMain || typeof ipcMain.handle !== "function" || typeof ipcMain.removeHandler !== "function") {
      throw new TypeError("companion_vision_ipc_invalid");
    }
    if (typeof requireMainRenderer !== "function" || typeof visionProvider !== "function") {
      throw new TypeError("companion_vision_boundary_invalid");
    }
    this.ipcMain = ipcMain;
    this.requireMainRenderer = requireMainRenderer;
    this.visionProvider = visionProvider;
    this.installed = false;
  }

  install() {
    if (this.installed) return false;
    const handlers = new Map([
      [CHANNELS[0], (event) => this.listSources(event)],
      [CHANNELS[1], (event, payload) => this.capture(event, payload)],
      [CHANNELS[2], (event, payload) => this.confirm(event, payload)],
      [CHANNELS[3], (event) => this.cancel(event)],
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

  listSources(event) {
    this.requireMainRenderer(event);
    return this.available().listSources();
  }

  capture(event, payload) {
    this.requireMainRenderer(event);
    return this.available().captureSource(payload);
  }

  confirm(event, payload) {
    this.requireMainRenderer(event);
    return this.available().confirm(payload);
  }

  cancel(event) {
    this.requireMainRenderer(event);
    return Object.freeze({ cancelled: this.visionProvider()?.cancel() === true });
  }

  available() {
    const vision = this.visionProvider();
    if (!vision) throw new Error("screen_vision_unavailable");
    return vision;
  }

  dispose() {
    if (!this.installed) return false;
    for (const channel of CHANNELS) this.ipcMain.removeHandler(channel);
    this.installed = false;
    return true;
  }
}

module.exports = { CHANNELS, CompanionScreenVisionIpcController };
