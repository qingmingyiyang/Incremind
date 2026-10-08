const CHANNELS = Object.freeze([
  "chriptmas:companion-routine-status",
  "chriptmas:companion-routine-refresh",
  "chriptmas:companion-routine-wake",
]);

class CompanionRoutineIpcController {
  constructor({ ipcMain, requireMainRenderer, routineController, refreshRoutine, presentOverlay, now = Date.now }) {
    if (!ipcMain || typeof ipcMain.handle !== "function" || typeof ipcMain.removeHandler !== "function") {
      throw new TypeError("companion_routine_ipc_invalid");
    }
    if (typeof requireMainRenderer !== "function" || !routineController
      || typeof routineController.status !== "function" || typeof routineController.wakeForThirtyMinutes !== "function"
      || typeof refreshRoutine !== "function" || typeof presentOverlay !== "function" || typeof now !== "function") {
      throw new TypeError("companion_routine_boundary_invalid");
    }
    this.ipcMain = ipcMain;
    this.requireMainRenderer = requireMainRenderer;
    this.routineController = routineController;
    this.refreshRoutine = refreshRoutine;
    this.presentOverlay = presentOverlay;
    this.now = now;
    this.installed = false;
  }

  install() {
    if (this.installed) return false;
    const handlers = new Map([
      [CHANNELS[0], (event) => this.status(event)],
      [CHANNELS[1], (event) => this.refresh(event)],
      [CHANNELS[2], (event) => this.wake(event)],
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
    return this.routineController.status();
  }

  refresh(event) {
    this.requireMainRenderer(event);
    return this.refreshRoutine();
  }

  wake(event) {
    this.requireMainRenderer(event);
    const result = this.routineController.wakeForThirtyMinutes();
    if (result.status === "awakened") {
      this.presentOverlay({
        event_id: `routine:wake:${this.now().toString(36)}`,
        kind: "routine_wake",
        visual_state: "attention",
        text: "我先醒来陪你半小时。",
        actions: [],
        requires_ack: false,
      }, { focus: false });
    }
    return result;
  }

  dispose() {
    if (!this.installed) return false;
    for (const channel of CHANNELS) this.ipcMain.removeHandler(channel);
    this.installed = false;
    return true;
  }
}

module.exports = { CHANNELS, CompanionRoutineIpcController };
