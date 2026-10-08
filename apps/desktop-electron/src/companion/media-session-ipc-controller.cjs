const CHANNELS = Object.freeze([
  "chriptmas:companion-media-refresh",
  "chriptmas:companion-media-status",
]);

const READY_CHANNEL = "chriptmas:companion-media-ready";
const PROJECTION_CHANNEL = "chriptmas:companion-media-session";

class CompanionMediaSessionIpcController {
  constructor({ ipcMain, requireMainRenderer, mediaController, mainWindowProvider, petWindowProvider }) {
    if (!ipcMain || typeof ipcMain.handle !== "function" || typeof ipcMain.removeHandler !== "function"
      || typeof ipcMain.on !== "function" || typeof ipcMain.removeListener !== "function") {
      throw new TypeError("companion_media_ipc_invalid");
    }
    if (typeof requireMainRenderer !== "function" || !mediaController
      || typeof mediaController.refreshConfig !== "function" || typeof mediaController.current !== "function"
      || typeof mainWindowProvider !== "function" || typeof petWindowProvider !== "function") {
      throw new TypeError("companion_media_boundary_invalid");
    }
    this.ipcMain = ipcMain;
    this.requireMainRenderer = requireMainRenderer;
    this.mediaController = mediaController;
    this.mainWindowProvider = mainWindowProvider;
    this.petWindowProvider = petWindowProvider;
    this.readyListener = (event) => this.ready(event);
    this.installed = false;
  }

  install() {
    if (this.installed) return false;
    const handlers = new Map([
      [CHANNELS[0], (event) => this.refresh(event)],
      [CHANNELS[1], (event) => this.status(event)],
    ]);
    const registered = [];
    try {
      for (const [channel, handler] of handlers) {
        this.ipcMain.handle(channel, handler);
        registered.push(channel);
      }
      this.ipcMain.on(READY_CHANNEL, this.readyListener);
    } catch (error) {
      this.ipcMain.removeListener(READY_CHANNEL, this.readyListener);
      for (const channel of registered) this.ipcMain.removeHandler(channel);
      throw error;
    }
    this.installed = true;
    return true;
  }

  refresh(event) {
    this.requireMainRenderer(event);
    return this.mediaController.refreshConfig();
  }

  status(event) {
    this.requireMainRenderer(event);
    return this.mediaController.current();
  }

  ready(event) {
    const sender = event?.sender;
    const isMain = isCurrentWindowSender(sender, this.mainWindowProvider());
    const isPet = isCurrentWindowSender(sender, this.petWindowProvider());
    if (!isMain && !isPet) return false;
    sender.send(PROJECTION_CHANNEL, this.mediaController.current());
    return true;
  }

  dispose() {
    if (!this.installed) return false;
    this.ipcMain.removeListener(READY_CHANNEL, this.readyListener);
    for (const channel of CHANNELS) this.ipcMain.removeHandler(channel);
    this.installed = false;
    return true;
  }
}

function isCurrentWindowSender(sender, window) {
  return Boolean(sender && window && !window.isDestroyed() && sender.id === window.webContents?.id);
}

module.exports = {
  CHANNELS,
  READY_CHANNEL,
  PROJECTION_CHANNEL,
  CompanionMediaSessionIpcController,
  isCurrentWindowSender,
};
