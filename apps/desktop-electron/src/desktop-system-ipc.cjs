const CHANNELS = Object.freeze([
  "chriptmas:platform-info",
  "chriptmas:open-path",
  "chriptmas:show-notification",
  "chriptmas:register-shortcut",
  "chriptmas:auto-update-status",
  "chriptmas:window-appearance",
]);

function safeShortText(value, fallback) {
  if (typeof value !== "string") return fallback;
  return value.replace(/\s+/g, " ").trim().slice(0, 180) || fallback;
}

class DesktopSystemIpcController {
  constructor({
    ipcMain,
    app,
    Notification,
    globalShortcut,
    shell,
    requireMainRenderer,
    showMainWindow,
    setWindowAppearance,
    candidateIdentityProvider = () => Object.freeze({ status: "not_run", reason: "identity_provider_unconfigured" }),
    platform = process.platform,
    pathSeparator,
  }) {
    if (!ipcMain || typeof ipcMain.handle !== "function" || typeof ipcMain.removeHandler !== "function") {
      throw new TypeError("desktop_system_ipc_invalid");
    }
    if (!app || typeof app.getPath !== "function") throw new TypeError("desktop_system_app_invalid");
    if (!Notification || typeof Notification.isSupported !== "function") {
      throw new TypeError("desktop_system_notification_invalid");
    }
    if (!globalShortcut || typeof globalShortcut.register !== "function"
      || typeof globalShortcut.unregister !== "function"
      || typeof globalShortcut.unregisterAll !== "function") {
      throw new TypeError("desktop_system_shortcut_invalid");
    }
    if (!shell || typeof shell.openPath !== "function") throw new TypeError("desktop_system_shell_invalid");
    if (typeof requireMainRenderer !== "function" || typeof showMainWindow !== "function" || typeof setWindowAppearance !== "function" || typeof candidateIdentityProvider !== "function") {
      throw new TypeError("desktop_system_boundary_invalid");
    }
    if (typeof platform !== "string" || typeof pathSeparator !== "string") {
      throw new TypeError("desktop_system_platform_invalid");
    }

    this.ipcMain = ipcMain;
    this.app = app;
    this.Notification = Notification;
    this.globalShortcut = globalShortcut;
    this.shell = shell;
    this.requireMainRenderer = requireMainRenderer;
    this.showMainWindow = showMainWindow;
    this.setWindowAppearance = setWindowAppearance;
    this.candidateIdentityProvider = candidateIdentityProvider;
    this.platform = platform;
    this.pathSeparator = pathSeparator;
    this.installed = false;
  }

  install() {
    if (this.installed) return false;
    const handlers = new Map([
      ["chriptmas:platform-info", (event) => this.platformInfo(event)],
      ["chriptmas:open-path", (event, options) => this.openPath(event, options)],
      ["chriptmas:show-notification", (event, options) => this.showNotification(event, options)],
      ["chriptmas:register-shortcut", (event, options) => this.registerShortcut(event, options)],
      ["chriptmas:auto-update-status", (event) => this.autoUpdateStatus(event)],
      ["chriptmas:window-appearance", (event, options) => this.windowAppearance(event, options)],
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

  platformInfo(event) {
    this.requireMainRenderer(event);
    return {
      appDataDir: this.app.getPath("userData"),
      pathSeparator: this.pathSeparator,
      platform: this.platform,
      shell: "electron",
      version: typeof this.app.getVersion === "function" ? this.app.getVersion() : "",
      candidate: this.candidateIdentityProvider(),
    };
  }

  async openPath(event, options = {}) {
    this.requireMainRenderer(event);
    const targetPath = typeof options?.path === "string" ? options.path.trim() : "";
    if (!targetPath || targetPath.includes("\0")) {
      return { status: "rejected", reason: "path is required" };
    }
    const result = await this.shell.openPath(targetPath);
    return result ? { status: "failed", reason: result } : { status: "opened" };
  }

  showNotification(event, options = {}) {
    this.requireMainRenderer(event);
    const title = safeShortText(options?.title, "Chriptmas OS");
    const body = safeShortText(options?.body, "");
    if (!this.Notification.isSupported()) return { status: "unsupported" };
    new this.Notification({ title, body }).show();
    return { status: "shown" };
  }

  registerShortcut(event, options = {}) {
    this.requireMainRenderer(event);
    const accelerator = typeof options?.accelerator === "string" ? options.accelerator.trim() : "";
    if (!accelerator || accelerator.length > 64 || accelerator.includes("\0")) {
      return { status: "rejected", reason: "invalid accelerator" };
    }
    this.globalShortcut.unregister(accelerator);
    const registered = this.globalShortcut.register(accelerator, () => this.showMainWindow());
    return { status: registered ? "registered" : "failed" };
  }

  autoUpdateStatus(event) {
    this.requireMainRenderer(event);
    return {
      status: "unavailable",
      reason: "auto-update is not configured",
      enabled: false,
    };
  }

  windowAppearance(event, options = {}) {
    this.requireMainRenderer(event);
    const theme = typeof options?.theme === "string" ? options.theme : "";
    if (!["light", "dark", "system"].includes(theme)) {
      return { status: "rejected", reason: "invalid theme" };
    }
    this.setWindowAppearance(theme);
    return { status: "applied", theme };
  }

  dispose() {
    if (!this.installed) return false;
    for (const channel of CHANNELS) this.ipcMain.removeHandler(channel);
    this.globalShortcut.unregisterAll();
    this.installed = false;
    return true;
  }
}

module.exports = { CHANNELS, DesktopSystemIpcController };
