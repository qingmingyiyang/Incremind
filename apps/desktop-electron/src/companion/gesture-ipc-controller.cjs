const EVENT_METHODS = Object.freeze([
  ["chriptmas:pet-gesture-begin", "begin"],
  ["chriptmas:pet-gesture-move", "move"],
  ["chriptmas:pet-gesture-end", "end"],
  ["chriptmas:pet-gesture-click", "click"],
]);

const CHANNELS = Object.freeze(EVENT_METHODS.map(([channel]) => channel));

class CompanionGestureIpcController {
  constructor({ ipcMain, petWindowProvider, gestureController }) {
    if (!ipcMain || typeof ipcMain.on !== "function" || typeof ipcMain.removeListener !== "function") {
      throw new TypeError("companion_gesture_ipc_invalid");
    }
    if (typeof petWindowProvider !== "function" || !gestureController
      || EVENT_METHODS.some(([, method]) => typeof gestureController[method] !== "function")) {
      throw new TypeError("companion_gesture_boundary_invalid");
    }
    this.ipcMain = ipcMain;
    this.petWindowProvider = petWindowProvider;
    this.gestureController = gestureController;
    this.listeners = new Map(EVENT_METHODS.map(([channel, method]) => [
      channel,
      (event, payload) => this.dispatch(event, method, payload),
    ]));
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

  dispatch(event, method, payload) {
    const petWindow = this.petWindowProvider();
    if (!petWindow || petWindow.isDestroyed() || event?.sender?.id !== petWindow.webContents?.id) return false;
    try {
      this.gestureController[method](payload);
      return true;
    } catch {
      return false;
    }
  }

  dispose() {
    if (!this.installed) return false;
    for (const [channel, listener] of this.listeners) this.ipcMain.removeListener(channel, listener);
    this.installed = false;
    return true;
  }
}

module.exports = { CHANNELS, EVENT_METHODS, CompanionGestureIpcController };
