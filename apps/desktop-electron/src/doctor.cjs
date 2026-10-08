const fs = require("node:fs");
const path = require("node:path");
const { resolveFrontendEntry } = require("./frontend-entry.cjs");

const ELECTRON_ROOT = path.resolve(__dirname, "..");
const ELECTRON_PACKAGE_ROOT = path.join(ELECTRON_ROOT, "node_modules", "electron");
const DEFAULT_FRONTEND_URL = "http://127.0.0.1:4173/?view=home";

function checkDesktopWorkspaceEntry() {
  const frontendUrl = process.env.CHRIPTMAS_REPLAY_FRONTEND_URL || DEFAULT_FRONTEND_URL;
  const checks = [
    checkPackageScript(),
    checkMainEntry(),
    checkPreloadEntry(),
    checkFrontendUrl(frontendUrl),
    checkElectronPackage(),
    checkElectronBinary(),
    checkPlatformAdapterMain(),
    checkPlatformAdapterPreload(),
  ];
  const blocking = checks.filter((check) => check.status !== "passed");
  return {
    status: blocking.length === 0 ? "passed" : "degraded",
    frontend_url: frontendUrl,
    entry_route: "home",
    checks,
    next_step: blocking.length === 0
      ? "run the Vite frontend server, then npm run dev; Electron opens the MVP workspace and verifies its own sidecar"
      : "fix the failed Electron entry checks before claiming real window smoke",
  };
}

function checkPackageScript() {
  const packagePath = path.join(ELECTRON_ROOT, "package.json");
  const packageJson = readJson(packagePath);
  const hasDev = packageJson?.scripts?.dev === "electron .";
  return check("package_dev_script", hasDev, packagePath, "package.json must expose dev: electron .");
}

function checkMainEntry() {
  const mainPath = path.join(ELECTRON_ROOT, "src", "main.cjs");
  const factoryPath = path.join(ELECTRON_ROOT, "src", "desktop-window-factory.cjs");
  const entryPath = path.join(ELECTRON_ROOT, "src", "frontend-entry.cjs");
  const securityPath = path.join(ELECTRON_ROOT, "src", "renderer-security.cjs");
  const main = readText(mainPath);
  const factory = readText(factoryPath);
  const entrySource = readText(entryPath);
  const security = readText(securityPath);
  const devMain = resolveFrontendEntry({ devUrl: DEFAULT_FRONTEND_URL, frontendIndex: "index.html" });
  const devPet = resolveFrontendEntry({ devUrl: `${DEFAULT_FRONTEND_URL}#view=rebuild-developer-studio`, petView: true, frontendIndex: "index.html" });
  const packagedMain = resolveFrontendEntry({ frontendIndex: "index.html" });
  const passed = main.includes('require("./frontend-entry.cjs")')
    && main.includes('require("./desktop-window-factory.cjs")')
    && entrySource.includes('DEFAULT_PET_VIEW = "rebuild-pet"')
    && new URL(devMain.value).hash === "#view=home"
    && new URL(devPet.value).hash === "#view=rebuild-pet"
    && packagedMain.hash === "view=home"
    && factory.includes("contextIsolation: true")
    && factory.includes("nodeIntegration: false")
    && factory.includes("sandbox: true")
    && delegatedNavigationPolicyReady(main, factory, security);
  return check("main_security_and_route", passed, `${mainPath}; ${factoryPath}; ${entryPath}; ${securityPath}`, "Electron entries must isolate home/pet hash routes with safe webPreferences and delegated navigation policy");
}

function delegatedNavigationPolicyReady(main, factory, security) {
  return main.includes('require("./desktop-window-factory.cjs")')
    && factory.includes('require("./renderer-security.cjs")')
    && factory.includes("installNavigationPolicy = installRendererNavigationPolicy")
    && factory.includes("this.#installNavigationPolicy(")
    && security.includes('setWindowOpenHandler(() => ({ action: "deny" }))')
    && security.includes('webContents.on("will-navigate"');
}

function checkPreloadEntry() {
  const preloadPath = path.join(ELECTRON_ROOT, "src", "preload.cjs");
  const preload = readText(preloadPath);
  const passed = preload.includes("contextBridge.exposeInMainWorld")
    && preload.includes("backendBaseUrl")
    && preload.includes("shell: \"electron\"")
    && preload.includes("entry: \"workspace\"")
    && preload.includes("selectLocalFile")
    && preload.includes("getPlatformInfo")
    && preload.includes("openPath")
    && preload.includes("showNotification")
    && preload.includes("registerShortcut")
    && preload.includes("getAutoUpdateStatus")
    && preload.includes("chriptmas:select-local-file")
    && preload.includes("chriptmas:platform-info")
    && preload.includes("chriptmas:open-path")
    && preload.includes("chriptmas:show-notification")
    && preload.includes("chriptmas:register-shortcut")
    && preload.includes("chriptmas:auto-update-status")
    && !preload.includes("writeFile")
    && !preload.includes("spawn(")
    && !preload.includes("exec(");
  return check(
    "preload_platform_bridge",
    passed,
    preloadPath,
    "preload must expose readonly shell/backend metadata and the user-triggered file picker only",
  );
}

function checkPlatformAdapterMain() {
  const mainPath = path.join(ELECTRON_ROOT, "src", "main.cjs");
  const desktopSystemIpcPath = path.join(ELECTRON_ROOT, "src", "desktop-system-ipc.cjs");
  const main = readText(mainPath);
  const desktopSystemIpc = readText(desktopSystemIpcPath);
  const passed = main.includes('require("./desktop-system-ipc.cjs")')
    && main.includes("new DesktopSystemIpcController({")
    && main.includes("requireMainRenderer,")
    && main.includes("pathSeparator: path.sep")
    && desktopSystemIpc.includes('this.app.getPath("userData")')
    && desktopSystemIpc.includes("chriptmas:open-path")
    && desktopSystemIpc.includes("this.shell.openPath")
    && desktopSystemIpc.includes("this.Notification.isSupported")
    && desktopSystemIpc.includes("this.globalShortcut.register")
    && desktopSystemIpc.includes("this.globalShortcut.unregisterAll")
    && desktopSystemIpc.includes("this.requireMainRenderer(event)")
    && desktopSystemIpc.includes("chriptmas:platform-info")
    && desktopSystemIpc.includes("chriptmas:show-notification")
    && desktopSystemIpc.includes("chriptmas:register-shortcut")
    && desktopSystemIpc.includes("chriptmas:auto-update-status");
  return check(
    "platform_adapter_main",
    passed,
    `${mainPath}; ${desktopSystemIpcPath}`,
    "main composition and desktop system controller must provide guarded appData, openPath, notification, shortcut, auto-update, and path IPC",
  );
}

function checkPlatformAdapterPreload() {
  const preloadPath = path.join(ELECTRON_ROOT, "src", "preload.cjs");
  const preload = readText(preloadPath);
  const passed = preload.includes("getPlatformInfo")
    && preload.includes("openPath")
    && preload.includes("showNotification")
    && preload.includes("registerShortcut")
    && preload.includes("getAutoUpdateStatus")
    && preload.includes("chriptmas:platform-info")
    && preload.includes("chriptmas:open-path")
    && preload.includes("chriptmas:show-notification")
    && preload.includes("chriptmas:register-shortcut")
    && preload.includes("chriptmas:auto-update-status");
  return check("platform_adapter_preload", passed, preloadPath, "preload must expose a narrow platform adapter bridge including auto-update placeholder");
}

function checkFrontendUrl(frontendUrl) {
  let passed = false;
  let evidence = frontendUrl;
  try {
    const url = new URL(frontendUrl);
    passed = url.searchParams.get("view") === "home";
  } catch (error) {
    evidence = error.message;
  }
  return check("frontend_url_route", passed, evidence, "frontend URL must include view=home");
}

function checkElectronPackage() {
  const packagePath = path.join(ELECTRON_PACKAGE_ROOT, "package.json");
  return check("electron_package_installed", fs.existsSync(packagePath), packagePath, "run npm install in apps/desktop-electron");
}

function checkElectronBinary() {
  const pathFile = path.join(ELECTRON_PACKAGE_ROOT, "path.txt");
  if (!fs.existsSync(pathFile)) {
    return check("electron_binary_available", false, pathFile, "Electron path.txt is missing; npm install did not fetch the binary");
  }
  const executableName = fs.readFileSync(pathFile, "utf-8").trim();
  const executablePath = path.join(ELECTRON_PACKAGE_ROOT, "dist", executableName);
  return check("electron_binary_available", fs.existsSync(executablePath), executablePath, "Electron binary is missing; reinstall Electron or check network/proxy");
}

function check(name, passed, evidence, message) {
  return {
    name,
    status: passed ? "passed" : "failed",
    evidence,
    message: passed ? "" : message,
  };
}

function readText(filePath) {
  try {
    return fs.readFileSync(filePath, "utf-8");
  } catch {
    return "";
  }
}

function readJson(filePath) {
  try {
    return JSON.parse(readText(filePath));
  } catch {
    return null;
  }
}

if (require.main === module) {
  const result = checkDesktopWorkspaceEntry();
  console.log(JSON.stringify(result, null, 2));
  process.exitCode = result.status === "passed" ? 0 : 1;
}

module.exports = {
  checkDesktopWorkspaceEntry,
};
