const CHANNEL = "chriptmas:companion-sensors-refresh";

class CompanionSystemSensorIpcController {
  constructor({ ipcMain, requireMainRenderer, sensorRuntime }) {
    if (!ipcMain || typeof ipcMain.handle !== "function" || typeof ipcMain.removeHandler !== "function") {
      throw new TypeError("companion_system_sensor_ipc_invalid");
    }
    if (typeof requireMainRenderer !== "function" || !sensorRuntime || typeof sensorRuntime.refresh !== "function") {
      throw new TypeError("companion_system_sensor_boundary_invalid");
    }
    this.ipcMain = ipcMain;
    this.requireMainRenderer = requireMainRenderer;
    this.sensorRuntime = sensorRuntime;
    this.installed = false;
  }

  install() {
    if (this.installed) return false;
    this.ipcMain.handle(CHANNEL, (event) => this.refresh(event));
    this.installed = true;
    return true;
  }

  refresh(event) {
    this.requireMainRenderer(event);
    return this.sensorRuntime.refresh();
  }

  dispose() {
    if (!this.installed) return false;
    this.ipcMain.removeHandler(CHANNEL);
    this.installed = false;
    return true;
  }
}

module.exports = { CHANNEL, CompanionSystemSensorIpcController };
