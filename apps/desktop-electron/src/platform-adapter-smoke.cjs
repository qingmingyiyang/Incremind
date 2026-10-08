const { app, BrowserWindow, ipcMain } = require("electron");
const path = require("node:path");

ipcMain.handle("chriptmas:platform-info", () => ({
  appDataDir: app.getPath("userData"),
  pathSeparator: path.sep,
  platform: process.platform,
  shell: "electron",
}));

ipcMain.handle("chriptmas:open-path", async (_event, options = {}) => {
  const targetPath = typeof options.path === "string" ? options.path.trim() : "";
  if (!targetPath) {
    return { status: "rejected", reason: "path is required" };
  }
  return { status: "opened" };
});

ipcMain.handle("chriptmas:show-notification", () => ({ status: "shown" }));
ipcMain.handle("chriptmas:register-shortcut", () => ({ status: "registered" }));
// spec 3.15：auto-update placeholder，始终返回 unavailable + enabled:false
ipcMain.handle("chriptmas:auto-update-status", () => ({
  status: "unavailable",
  reason: "auto-update is not configured",
  enabled: false,
}));

async function runPlatformAdapterSmoke() {
  await app.whenReady();
  const win = new BrowserWindow({
    width: 720,
    height: 420,
    show: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
    },
  });

  win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  await win.loadURL(smokePageUrl());
  const result = await win.webContents.executeJavaScript(
    "window.runSmoke().then((value) => value)",
    true,
  );
  console.log(JSON.stringify({
    status: result.passed ? "passed" : "failed",
    ...result,
  }, null, 2));
  win.close();
  app.quit();
  process.exitCode = result.passed ? 0 : 1;
}

function smokePageUrl() {
  const html = `<!doctype html>
    <html>
      <head>
        <meta charset="utf-8" />
        <title>Electron Platform Adapter Smoke</title>
      </head>
      <body>
        <main>Platform adapter smoke</main>
        <script>
          window.runSmoke = async function runSmoke() {
            const api = window.electronAPI;
            const hasBridge = Boolean(api && api.shell === "electron" && api.entry === "workspace");
            const info = hasBridge && typeof api.getPlatformInfo === "function"
              ? await api.getPlatformInfo()
              : {};
            const openRejected = hasBridge && typeof api.openPath === "function"
              ? await api.openPath("")
              : {};
            const notification = hasBridge && typeof api.showNotification === "function"
              ? await api.showNotification({ title: "Smoke", body: "Platform adapter" })
              : {};
            const shortcut = hasBridge && typeof api.registerShortcut === "function"
              ? await api.registerShortcut("Ctrl+Shift+Alt+R")
              : {};
            const autoUpdate = hasBridge && typeof api.getAutoUpdateStatus === "function"
              ? await api.getAutoUpdateStatus()
              : {};
            return {
              passed: Boolean(
                hasBridge
                && info.shell === "electron"
                && typeof info.appDataDir === "string"
                && info.appDataDir.length > 0
                && typeof info.pathSeparator === "string"
                && openRejected.status === "rejected"
                && notification.status === "shown"
                && shortcut.status === "registered"
                && autoUpdate.status === "unavailable"
                && autoUpdate.enabled === false
                && typeof window.require !== "function"
                && typeof window.process === "undefined"
              ),
              has_bridge: hasBridge,
              app_data_dir_present: typeof info.appDataDir === "string" && info.appDataDir.length > 0,
              path_separator: info.pathSeparator,
              open_empty_path_status: openRejected.status,
              notification_status: notification.status,
              shortcut_status: shortcut.status,
              auto_update_status: autoUpdate.status,
              auto_update_enabled: autoUpdate.enabled,
              node_isolated: typeof window.require !== "function" && typeof window.process === "undefined",
            };
          };
        </script>
      </body>
    </html>`;
  return `data:text/html;charset=utf-8,${encodeURIComponent(html)}`;
}

runPlatformAdapterSmoke().catch((error) => {
  console.error(JSON.stringify({
    status: "failed",
    error: error.message,
  }, null, 2));
  app.quit();
  process.exitCode = 1;
});
