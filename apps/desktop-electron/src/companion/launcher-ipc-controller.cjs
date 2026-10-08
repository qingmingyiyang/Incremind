const CHANNELS = Object.freeze([
  "chriptmas:companion-launchers-list",
  "chriptmas:companion-launcher-add-program",
  "chriptmas:companion-launcher-add-bookmark",
  "chriptmas:companion-launcher-rename",
  "chriptmas:companion-launcher-delete",
  "chriptmas:companion-launcher-open",
]);

function requireExactPayload(payload, keys) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("companion_launcher_payload_rejected");
  }
  const actualKeys = Object.keys(payload);
  if (actualKeys.length !== keys.length || !keys.every((key) => actualKeys.includes(key))) {
    throw new Error("companion_launcher_payload_rejected");
  }
  for (const key of keys) {
    if (typeof payload[key] !== "string") throw new Error("companion_launcher_payload_rejected");
  }
  return payload;
}

class CompanionLauncherIpcController {
  constructor({ ipcMain, dialog, requireMainRenderer, mainWindowProvider, launcherProvider, onLaunched }) {
    if (!ipcMain || typeof ipcMain.handle !== "function" || typeof ipcMain.removeHandler !== "function") {
      throw new TypeError("companion_launcher_ipc_invalid");
    }
    if (!dialog || typeof dialog.showOpenDialog !== "function") {
      throw new TypeError("companion_launcher_dialog_invalid");
    }
    if (typeof requireMainRenderer !== "function" || typeof mainWindowProvider !== "function"
      || typeof launcherProvider !== "function" || typeof onLaunched !== "function") {
      throw new TypeError("companion_launcher_boundary_invalid");
    }
    this.ipcMain = ipcMain;
    this.dialog = dialog;
    this.requireMainRenderer = requireMainRenderer;
    this.mainWindowProvider = mainWindowProvider;
    this.launcherProvider = launcherProvider;
    this.onLaunched = onLaunched;
    this.installed = false;
  }

  install() {
    if (this.installed) return false;
    const handlers = new Map([
      [CHANNELS[0], (event) => this.list(event)],
      [CHANNELS[1], (event, payload) => this.addProgram(event, payload)],
      [CHANNELS[2], (event, payload) => this.addBookmark(event, payload)],
      [CHANNELS[3], (event, payload) => this.rename(event, payload)],
      [CHANNELS[4], (event, payload) => this.remove(event, payload)],
      [CHANNELS[5], (event, payload) => this.launch(event, payload)],
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

  list(event) {
    this.requireMainRenderer(event);
    return this.launcherProvider()?.list() || { state: "unavailable", entries: [] };
  }

  async addProgram(event, payload) {
    this.requireMainRenderer(event);
    const { name } = requireExactPayload(payload, ["name"]);
    const launcher = this.available();
    const selection = await this.dialog.showOpenDialog(this.mainWindowProvider(), {
      title: "选择要登记的程序",
      properties: ["openFile"],
      filters: [{ name: "Windows programs", extensions: ["exe", "com"] }],
    });
    if (selection.canceled || selection.filePaths.length !== 1) return { status: "cancelled" };
    return { status: "created", entry: launcher.addProgram({ name, selectedPath: selection.filePaths[0] }) };
  }

  addBookmark(event, payload) {
    this.requireMainRenderer(event);
    const launcher = this.available();
    return { status: "created", entry: launcher.addBookmark(requireExactPayload(payload, ["name", "url"])) };
  }

  rename(event, payload) {
    this.requireMainRenderer(event);
    const launcher = this.available();
    return { status: "renamed", entry: launcher.rename(requireExactPayload(payload, ["id", "name"])) };
  }

  remove(event, payload) {
    this.requireMainRenderer(event);
    const launcher = this.available();
    return launcher.remove(requireExactPayload(payload, ["id"]));
  }

  async launch(event, payload) {
    this.requireMainRenderer(event);
    const launcher = this.available();
    const result = await launcher.launch(requireExactPayload(payload, ["id"]));
    this.onLaunched(result);
    return result;
  }

  available() {
    const launcher = this.launcherProvider();
    if (!launcher) throw new Error("companion_launcher_unavailable");
    return launcher;
  }

  dispose() {
    if (!this.installed) return false;
    for (const channel of CHANNELS) this.ipcMain.removeHandler(channel);
    this.installed = false;
    return true;
  }
}

module.exports = { CHANNELS, CompanionLauncherIpcController, requireExactPayload };
