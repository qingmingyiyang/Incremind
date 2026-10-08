const EVENTS = Object.freeze([
  "chriptmas:companion-state-ready",
  "chriptmas:companion-appearance-ready",
]);

class CompanionProjectionReadyIpcController {
  constructor({ ipcMain, petWindowProvider, stateController, appearanceArbiter, appearanceRuntimeController }) {
    if (!ipcMain || typeof ipcMain.on !== "function" || typeof ipcMain.removeListener !== "function") {
      throw new TypeError("companion_projection_ready_ipc_invalid");
    }
    if (typeof petWindowProvider !== "function"
      || !stateController || typeof stateController.deliverCurrent !== "function"
      || !appearanceArbiter || typeof appearanceArbiter.deliverCurrent !== "function"
      || !appearanceRuntimeController || typeof appearanceRuntimeController.deliverCurrent !== "function") {
      throw new TypeError("companion_projection_ready_boundary_invalid");
    }
    this.ipcMain = ipcMain;
    this.petWindowProvider = petWindowProvider;
    this.stateController = stateController;
    this.appearanceArbiter = appearanceArbiter;
    this.appearanceRuntimeController = appearanceRuntimeController;
    this.listeners = new Map([
      [EVENTS[0], (event) => this.stateReady(event)],
      [EVENTS[1], (event) => this.appearanceReady(event)],
    ]);
    this.installed = false;
  }

  install() {
    if (this.installed) return false;
    const registered = [];
    try {
      for (const [channel, listener] of this.listeners) {
        this.ipcMain.on(channel, listener);
        registered.push([channel, listener]);
      }
    } catch (error) {
      for (const [channel, listener] of registered) this.ipcMain.removeListener(channel, listener);
      throw error;
    }
    this.installed = true;
    return true;
  }

  stateReady(event) {
    const petWindow = this.petWindowProvider();
    if (!petWindow || petWindow.isDestroyed() || event?.sender?.id !== petWindow.webContents?.id) return false;
    this.stateController.deliverCurrent();
    this.appearanceArbiter.deliverCurrent();
    return true;
  }

  appearanceReady(event) {
    const petWindow = this.petWindowProvider();
    if (!petWindow || petWindow.isDestroyed() || event?.sender !== petWindow.webContents
      || event?.senderFrame !== petWindow.webContents?.mainFrame) return false;
    this.appearanceRuntimeController.deliverCurrent();
    return true;
  }

  dispose() {
    if (!this.installed) return false;
    for (const [channel, listener] of this.listeners) this.ipcMain.removeListener(channel, listener);
    this.installed = false;
    return true;
  }
}

module.exports = { EVENTS, CompanionProjectionReadyIpcController };
