const READY_CHANNEL = "chriptmas:companion-overlay-ready";
const EVENT_CHANNEL = "chriptmas:companion-overlay-event";
const CHANNELS = Object.freeze([
  "chriptmas:companion-overlay-submit",
  "chriptmas:companion-overlay-action",
  "chriptmas:companion-overlay-ack",
  "chriptmas:companion-overlay-close",
  "chriptmas:companion-overlay-open-center",
]);

class CompanionOverlayIpcController {
  constructor({ ipcMain, overlayWindowProvider, overlayController, validatePanelPayload, openCenter }) {
    if (!ipcMain || typeof ipcMain.handle !== "function" || typeof ipcMain.removeHandler !== "function"
      || typeof ipcMain.on !== "function" || typeof ipcMain.removeListener !== "function") {
      throw new TypeError("companion_overlay_ipc_invalid");
    }
    if (typeof overlayWindowProvider !== "function" || typeof validatePanelPayload !== "function"
      || typeof openCenter !== "function") {
      throw new TypeError("companion_overlay_boundary_invalid");
    }
    if (!overlayController || typeof overlayController.submit !== "function" || typeof overlayController.perform !== "function"
      || typeof overlayController.acknowledge !== "function" || typeof overlayController.close !== "function") {
      throw new TypeError("companion_overlay_controller_invalid");
    }
    this.ipcMain = ipcMain;
    this.overlayWindowProvider = overlayWindowProvider;
    this.overlayController = overlayController;
    this.validatePanelPayload = validatePanelPayload;
    this.openCenter = openCenter;
    this.readyListener = (event) => this.ready(event);
    this.installed = false;
  }

  install() {
    if (this.installed) return false;
    const handlers = new Map([
      [CHANNELS[0], (event, payload) => this.submit(event, payload)],
      [CHANNELS[1], (event, payload) => this.perform(event, payload)],
      [CHANNELS[2], (event, payload) => this.acknowledge(event, payload)],
      [CHANNELS[3], (event) => this.close(event)],
      [CHANNELS[4], (event, payload) => this.openCompanionCenter(event, payload)],
    ]);
    const registered = [];
    let readyRegistered = false;
    try {
      for (const [channel, handler] of handlers) {
        this.ipcMain.handle(channel, handler);
        registered.push(channel);
      }
      this.ipcMain.on(READY_CHANNEL, this.readyListener);
      readyRegistered = true;
    } catch (error) {
      if (readyRegistered) this.ipcMain.removeListener(READY_CHANNEL, this.readyListener);
      for (const channel of registered) this.ipcMain.removeHandler(channel);
      throw error;
    }
    this.installed = true;
    return true;
  }

  ready(event) {
    if (!this.isOverlayRenderer(event)) return false;
    if (this.overlayController.current) event.sender.send(EVENT_CHANNEL, this.overlayController.current);
    return true;
  }

  submit(event, payload) {
    this.requireOverlayRenderer(event);
    return this.overlayController.submit(payload);
  }

  perform(event, payload) {
    this.requireOverlayRenderer(event);
    return this.overlayController.perform(payload);
  }

  acknowledge(event, payload) {
    this.requireOverlayRenderer(event);
    return this.overlayController.acknowledge(payload);
  }

  close(event) {
    this.requireOverlayRenderer(event);
    return this.overlayController.close();
  }

  openCompanionCenter(event, payload) {
    this.requireOverlayRenderer(event);
    return this.openCenter(this.validatePanelPayload(payload).panel);
  }

  requireOverlayRenderer(event) {
    if (!this.isOverlayRenderer(event)) {
      throw new Error("ipc_overlay_sender_rejected");
    }
  }

  isOverlayRenderer(event) {
    const overlayWindow = this.overlayWindowProvider();
    return Boolean(overlayWindow && !overlayWindow.isDestroyed() && event?.sender?.id === overlayWindow.webContents.id);
  }

  dispose() {
    if (!this.installed) return false;
    this.ipcMain.removeListener(READY_CHANNEL, this.readyListener);
    for (const channel of CHANNELS) this.ipcMain.removeHandler(channel);
    this.installed = false;
    return true;
  }
}

module.exports = { CHANNELS, EVENT_CHANNEL, READY_CHANNEL, CompanionOverlayIpcController };
