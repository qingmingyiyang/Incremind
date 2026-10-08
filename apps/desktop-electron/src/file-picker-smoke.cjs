const { app, BrowserWindow, ipcMain } = require("electron");
const path = require("node:path");

const smokePath = process.env.CHRIPTMAS_ELECTRON_SMOKE_PICKER_PATH || "D:\\media\\smoke-document.pdf";

ipcMain.handle("chriptmas:select-local-file", async (_event, options = {}) => {
  const mediaKind = typeof options.mediaKind === "string" ? options.mediaKind : "file";
  return `${smokePath}#${mediaKind}`;
});

async function runFilePickerSmoke() {
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
    `window.runSmoke().then((value) => value)`,
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
        <title>Electron File Picker Smoke</title>
      </head>
      <body>
        <main>
          <label>
            文档路径
            <input aria-label="文档本地授权路径" id="file-path" />
          </label>
          <button id="select-file" type="button">选择本地文件</button>
        </main>
        <script>
          window.runSmoke = async function runSmoke() {
            const api = window.electronAPI;
            const hasBridge = Boolean(api && api.shell === "electron" && api.entry === "workspace");
            const selected = hasBridge && typeof api.selectLocalFile === "function"
              ? await api.selectLocalFile({ mediaKind: "document" })
              : "";
            const input = document.getElementById("file-path");
            input.value = selected || "";
            return {
              passed: hasBridge && input.value.endsWith("#document"),
              has_bridge: hasBridge,
              selected_path: input.value,
              input_value: input.value,
              node_exposed: typeof window.require !== "function" && typeof window.process === "undefined",
            };
          };
        </script>
      </body>
    </html>`;
  return `data:text/html;charset=utf-8,${encodeURIComponent(html)}`;
}

runFilePickerSmoke().catch((error) => {
  console.error(JSON.stringify({
    status: "failed",
    error: error.message,
  }, null, 2));
  app.quit();
  process.exitCode = 1;
});
