const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const { createStartupWindow, startupDocument } = require("../src/startup-window.cjs");

test("startup document is local, script-free, escaped, and accessible", () => {
  const html = startupDocument('0.2.0<script src="https://example.com/x.js"></script>');
  assert.match(html, /default-src 'none'/);
  assert.match(html, /role="status"/);
  assert.match(html, /prefers-reduced-motion/);
  assert.match(html, /html, body[^}]+-webkit-app-region: drag/);
  assert.match(html, /button, a, input, select, textarea[^}]+-webkit-app-region: no-drag/);
  assert.doesNotMatch(html, /<script/i);
  assert.doesNotMatch(html, /<(?:script|link|iframe|img)\b[^>]*(?:src|href)=["']https?:\/\//i);
  assert.match(html, /&lt;script/);
});

test("startup window has no privileged bridge or navigation surface", () => {
  let options = null;
  let loadedUrl = "";
  let navigationHandler = null;
  let openHandler = null;
  class BrowserWindowMock {
    constructor(value) {
      options = value;
      this.webContents = {
        on(name, handler) { if (name === "will-navigate") navigationHandler = handler; },
        setWindowOpenHandler(handler) { openHandler = handler; },
      };
    }
    removeMenu() {}
    loadURL(value) { loadedUrl = value; }
  }
  createStartupWindow({ BrowserWindow: BrowserWindowMock, version: "0.2.0" });
  assert.equal(options.show, true);
  assert.equal(options.webPreferences.nodeIntegration, false);
  assert.equal(options.webPreferences.contextIsolation, true);
  assert.equal(options.webPreferences.sandbox, true);
  assert.equal(options.webPreferences.devTools, false);
  assert.equal(Object.hasOwn(options.webPreferences, "preload"), false);
  assert.match(loadedUrl, /^data:text\/html;charset=UTF-8,/);
  assert.deepEqual(openHandler(), { action: "deny" });
  let prevented = false;
  navigationHandler({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
});

test("main creates startup feedback before awaiting sidecar readiness", () => {
  const main = fs.readFileSync(path.join(__dirname, "..", "src", "main.cjs"), "utf8");
  const readyBlock = main.slice(main.indexOf("const applicationBootstrapCoordinator"), main.indexOf("applicationBootstrapCoordinator.install();"));
  const startupIndex = readyBlock.indexOf('beginStartupFeedback: () => desktopWindowRegistry.register("startup", createStartupWindow');
  const sidecarIndex = readyBlock.indexOf("startRequiredRuntime: () => desktopRuntimeBootstrapCoordinator.start()");
  const mainWindowIndex = readyBlock.indexOf('name: "main-window", run: createMainWindow');
  assert.ok(startupIndex >= 0 && startupIndex < sidecarIndex);
  assert.ok(sidecarIndex < mainWindowIndex);
  assert.doesNotMatch(main, /async function startSidecar|let sidecarSupervisor|let vaultRecoveryController/);
  assert.match(main, /createdMainWindow\.once\("ready-to-show", \(\) => \{[\s\S]*?desktopWindowRegistry\.destroy\("startup"\)/);
  assert.doesNotMatch(readyBlock, /createTray\(\);\s*desktopWindowRegistry\.destroy\("startup"\)/);
  assert.doesNotMatch(main, /let (?:mainWindow|petWindow|companionOverlayWindow|startupWindow)\s*=/);
  assert.match(main, /本地后端启动超过 45 秒，应用已停止等待/);
  assert.doesNotMatch(main, /本地后端首次启动超过 3 分钟/);
});

test("tray uses a packaged non-empty local icon", () => {
  const iconPath = path.join(__dirname, "..", "src", "tray-icon.png");
  const main = fs.readFileSync(path.join(__dirname, "..", "src", "main.cjs"), "utf8");
  const trayController = fs.readFileSync(path.join(__dirname, "..", "src", "desktop-tray-controller.cjs"), "utf8");
  assert.ok(fs.statSync(iconPath).size > 512);
  assert.match(main, /path\.join\(__dirname, "tray-icon\.png"\)/);
  assert.match(trayController, /\.resize\(\{ width: 32, height: 32, quality: "best" \}\)/);
  assert.match(trayController, /icon\.isEmpty\(\) \? this\.nativeImage\.createEmpty\(\) : icon/);
  assert.doesNotMatch(main, /let tray\s*=|function createTray/);
});

test("Windows package, executable, installer, and main window share the bear icon", () => {
  const root = path.join(__dirname, "..");
  const packageJson = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
  const windowFactory = fs.readFileSync(path.join(root, "src", "desktop-window-factory.cjs"), "utf8");
  const icoPath = path.join(root, packageJson.build.win.icon);
  assert.ok(fs.statSync(icoPath).size > 1024);
  assert.equal(packageJson.build.win.signExecutable, false);
  assert.equal(Object.hasOwn(packageJson.build.win, "signAndEditExecutable"), false);
  assert.equal(packageJson.build.nsis.installerIcon, packageJson.build.win.icon);
  assert.equal(packageJson.build.nsis.uninstallerIcon, packageJson.build.win.icon);
  assert.equal(packageJson.build.nsis.installerHeaderIcon, packageJson.build.win.icon);
  assert.match(windowFactory, /icon: path\.join\(this\.#baseDir, "tray-icon\.png"\)/);
});
