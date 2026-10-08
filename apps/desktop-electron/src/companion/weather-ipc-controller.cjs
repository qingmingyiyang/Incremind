const CHANNELS = Object.freeze([
  "chriptmas:companion-weather-refresh",
  "chriptmas:companion-weather-terms-open",
]);

const READY_CHANNEL = "chriptmas:companion-weather-ready";
const TERMS_URL = "https://open-meteo.com/en/terms";

class CompanionWeatherIpcController {
  constructor({ ipcMain, requireMainRenderer, weatherController, petWindowProvider, openExternal }) {
    if (!ipcMain || typeof ipcMain.handle !== "function" || typeof ipcMain.removeHandler !== "function"
      || typeof ipcMain.on !== "function" || typeof ipcMain.removeListener !== "function") {
      throw new TypeError("companion_weather_ipc_invalid");
    }
    if (typeof requireMainRenderer !== "function" || !weatherController
      || typeof weatherController.refresh !== "function" || typeof weatherController.current !== "function"
      || typeof petWindowProvider !== "function" || typeof openExternal !== "function") {
      throw new TypeError("companion_weather_boundary_invalid");
    }
    this.ipcMain = ipcMain;
    this.requireMainRenderer = requireMainRenderer;
    this.weatherController = weatherController;
    this.petWindowProvider = petWindowProvider;
    this.openExternal = openExternal;
    this.readyListener = (event) => this.ready(event);
    this.installed = false;
  }

  install() {
    if (this.installed) return false;
    const handlers = new Map([
      [CHANNELS[0], (event) => this.refresh(event)],
      [CHANNELS[1], (event) => this.openTerms(event)],
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
    return this.weatherController.refresh();
  }

  async openTerms(event) {
    this.requireMainRenderer(event);
    await this.openExternal(TERMS_URL);
    return Object.freeze({ status: "opened" });
  }

  ready(event) {
    const petWindow = this.petWindowProvider();
    if (!petWindow || petWindow.isDestroyed() || event?.sender?.id !== petWindow.webContents?.id) return false;
    petWindow.webContents.send("chriptmas:companion-weather", this.weatherController.current());
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

module.exports = { CHANNELS, READY_CHANNEL, TERMS_URL, CompanionWeatherIpcController };
