const { app, BrowserWindow, Notification, dialog, globalShortcut, ipcMain, shell, session, Tray, Menu, nativeImage } = require("electron");
const path = require("node:path");
const fs = require("node:fs");
const { SidecarSupervisor, SESSION_HEADER } = require("./sidecar-supervisor.cjs");

// ── 前端入口解析 ──
// 开发模式：设置 CHRIPTMAS_REPLAY_FRONTEND_URL 环境变量，用 loadURL 加载 dev server。
// 打包模式：不设置该变量，用 loadFile 加载本地 build 产物（frontend-dist/index.html）。
const FRONTEND_DIST_DIR = path.join(__dirname, "..", "frontend-dist");
const FRONTEND_DIST_INDEX = path.join(FRONTEND_DIST_DIR, "index.html");
const DEFAULT_PET_VIEW = "rebuild-pet";

function resolveFrontendEntry(petView = false) {
  const devUrl = process.env.CHRIPTMAS_REPLAY_FRONTEND_URL;
  if (devUrl) {
    // 开发模式：loadURL，宠物窗口复用同一 origin 但 view=rebuild-pet
    const url = new URL(devUrl);
    if (petView) {
      url.searchParams.set("view", DEFAULT_PET_VIEW);
    } else if (!url.searchParams.has("view")) {
      url.searchParams.set("view", "rebuild-self-use-alpha");
    }
    return { type: "url", value: url.toString() };
  }
  // 打包模式：loadFile 加载本地 build 产物，用 hash 路由
  if (petView) {
    return { type: "file", value: FRONTEND_DIST_INDEX, hash: `view=${DEFAULT_PET_VIEW}` };
  }
  return { type: "file", value: FRONTEND_DIST_INDEX, hash: "view=rebuild-self-use-alpha" };
}

// 后端地址（供 will-navigate 白名单使用；API base URL 由 preload.cjs 通过 electronAPI 暴露）
function backendOrigin() {
  return sidecarSupervisor?.session?.origin || "";
}

// 全局引用，避免被 GC 回收
let mainWindow = null;
let petWindow = null;
let tray = null;
let sidecarSupervisor = null;

function installSidecarRequestAuthentication() {
  session.defaultSession.webRequest.onBeforeSendHeaders((details, callback) => {
    const active = sidecarSupervisor?.session;
    if (!active || new URL(details.url).origin !== active.origin) return callback({ requestHeaders: details.requestHeaders });
    callback({ requestHeaders: { ...details.requestHeaders, [SESSION_HEADER]: active.secret } });
  });
}

async function startSidecar() {
  const rootDir = path.resolve(__dirname, "..", "..", "..");
  const sidecarRoot = app.isPackaged ? path.join(process.resourcesPath, "sidecar") : rootDir;
  const pythonPath = process.env.CHRIPTMAS_OS_PYTHON || path.join(sidecarRoot, "runtime", process.platform === "win32" ? "python.exe" : "python");
  const workingDir = app.isPackaged ? app.getPath("userData") : rootDir;
  if (app.isPackaged) {
    const settingsPath = path.join(workingDir, "config", "settings.toml");
    if (!fs.existsSync(settingsPath)) {
      fs.mkdirSync(path.dirname(settingsPath), { recursive: true });
      fs.copyFileSync(path.join(sidecarRoot, "config", "settings.toml"), settingsPath);
    }
  }
  sidecarSupervisor = new SidecarSupervisor({
    rootDir,
    moduleRoot: app.isPackaged ? sidecarRoot : path.join(rootDir, "src"),
    workingDir,
    pythonPath,
    log: (message) => console.warn(`[sidecar] ${message}`),
    onUnexpectedExit: () => {
      if (app.isQuitting) return;
      dialog.showMessageBox({
        type: "error",
        title: "本地后端已停止",
        message: "Chriptmas OS 的本地后端意外停止。重启应用会创建新的安全会话。",
        buttons: ["重启应用", "退出"],
        defaultId: 0,
        cancelId: 1,
        noLink: true,
      }).then(({ response }) => {
        if (response === 0) app.relaunch();
        app.exit();
      });
    },
  });
  const active = await sidecarSupervisor.start();
  process.env.CHRIPTMAS_DESKTOP_BACKEND_ORIGIN = active.origin;
  installSidecarRequestAuthentication();
}

function createMainWindow() {
  const entry = resolveFrontendEntry(false);
  mainWindow = new BrowserWindow({
    width: 1180,
    height: 820,
    minWidth: 960,
    minHeight: 680,
    title: "Chriptmas OS",
    show: false, // 启动时隐藏，点击宠物后才显示
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
    },
  });

  // 打包模式下 file:// origin，开发模式下 dev server origin
  const allowedOrigin = entry.type === "url" ? new URL(entry.value).origin : "file://";
  const backend = backendOrigin();

  mainWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  mainWindow.webContents.on("will-navigate", (event, targetUrl) => {
    const targetOrigin = new URL(targetUrl).origin;
    // 允许前端自身 origin 和后端 API origin
    if (targetOrigin !== allowedOrigin && targetOrigin !== backend) {
      event.preventDefault();
    }
  });

  if (entry.type === "url") {
    mainWindow.loadURL(entry.value);
  } else {
    mainWindow.loadFile(entry.value, entry.hash ? { hash: entry.hash } : undefined);
  }

  // 主窗口关闭时隐藏到托盘，而非退出应用
  mainWindow.on("close", (event) => {
    if (!app.isQuitting) {
      event.preventDefault();
      mainWindow.hide();
    }
  });

  return mainWindow;
}

function createPetWindow() {
  const entry = resolveFrontendEntry(true);
  petWindow = new BrowserWindow({
    width: 180,
    height: 220,
    frame: false,            // 无边框
    transparent: true,       // 透明背景
    resizable: false,        // 不可调整大小
    maximizable: false,      // 不可最大化
    minimizable: false,      // 不可最小化（直接隐藏）
    skipTaskbar: true,       // 不在任务栏显示
    alwaysOnTop: true,       // 置顶
    hasShadow: false,        // 无阴影（透明窗口自带阴影会显得脏）
    focusable: true,         // 可聚焦（接收点击）
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
    },
  });

  const allowedOrigin = entry.type === "url" ? new URL(entry.value).origin : "file://";
  const backend = backendOrigin();

  petWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  petWindow.webContents.on("will-navigate", (event, targetUrl) => {
    const targetOrigin = new URL(targetUrl).origin;
    if (targetOrigin !== allowedOrigin && targetOrigin !== backend) {
      event.preventDefault();
    }
  });

  // 透明窗口需要设置背景色为透明
  petWindow.setBackgroundColor("#00000000");
  if (entry.type === "url") {
    petWindow.loadURL(entry.value);
  } else {
    petWindow.loadFile(entry.value, entry.hash ? { hash: entry.hash } : undefined);
  }
  petWindow.setAlwaysOnTop(true, "floating", 1);

  // 宠物窗口关闭时直接退出应用（宠物是应用入口）
  petWindow.on("closed", () => {
    petWindow = null;
  });

  return petWindow;
}

function showMainWindow() {
  if (mainWindow === null) {
    createMainWindow();
  }
  if (mainWindow.isMinimized()) {
    mainWindow.restore();
  }
  mainWindow.show();
  mainWindow.focus();
}

function togglePetWindow() {
  if (petWindow === null) {
    createPetWindow();
    return;
  }
  if (petWindow.isVisible()) {
    petWindow.hide();
  } else {
    petWindow.show();
  }
}

function createTray() {
  // 使用空白图标作为托盘（后续可替换为小熊缩略图）
  // nativeImage.createEmpty() 在 Windows 上可能不显示，用 1x1 透明 PNG
  const iconPath = path.join(__dirname, "tray-icon.png");
  let trayIcon;
  try {
    trayIcon = nativeImage.createFromPath(iconPath);
    if (trayIcon.isEmpty()) {
      trayIcon = nativeImage.createEmpty();
    }
  } catch {
    trayIcon = nativeImage.createEmpty();
  }

  tray = new Tray(trayIcon);
  tray.setToolTip("Chriptmas OS");

  const contextMenu = Menu.buildFromTemplate([
    {
      label: "打开主界面",
      click: () => showMainWindow(),
    },
    {
      label: "显示/隐藏宠物",
      click: () => togglePetWindow(),
    },
    { type: "separator" },
    {
      label: "退出",
      click: () => {
        app.isQuitting = true;
        app.quit();
      },
    },
  ]);

  tray.setContextMenu(contextMenu);
  tray.on("click", () => {
    showMainWindow();
  });
}

ipcMain.handle("chriptmas:select-local-file", async (_event, options = {}) => {
  const mediaKind = typeof options.mediaKind === "string" ? options.mediaKind : "file";
  const result = await dialog.showOpenDialog({
    title: "选择本地文件",
    properties: ["openFile"],
    filters: localFileFilters(mediaKind),
  });
  if (result.canceled || result.filePaths.length === 0) {
    return null;
  }
  return result.filePaths[0];
});

ipcMain.handle("chriptmas:platform-info", () => ({
  appDataDir: app.getPath("userData"),
  pathSeparator: path.sep,
  platform: process.platform,
  shell: "electron",
}));

ipcMain.handle("chriptmas:open-path", async (_event, options = {}) => {
  const targetPath = typeof options.path === "string" ? options.path.trim() : "";
  if (!targetPath || targetPath.includes("\0")) {
    return { status: "rejected", reason: "path is required" };
  }
  const result = await shell.openPath(targetPath);
  if (result) {
    return { status: "failed", reason: result };
  }
  return { status: "opened" };
});

ipcMain.handle("chriptmas:show-notification", (_event, options = {}) => {
  const title = safeShortText(options.title, "Chriptmas OS");
  const body = safeShortText(options.body, "");
  if (!Notification.isSupported()) {
    return { status: "unsupported" };
  }
  new Notification({ title, body }).show();
  return { status: "shown" };
});

ipcMain.handle("chriptmas:register-shortcut", (_event, options = {}) => {
  const accelerator = typeof options.accelerator === "string" ? options.accelerator.trim() : "";
  if (!accelerator || accelerator.length > 64 || accelerator.includes("\0")) {
    return { status: "rejected", reason: "invalid accelerator" };
  }
  globalShortcut.unregister(accelerator);
  const registered = globalShortcut.register(accelerator, () => {
    showMainWindow();
  });
  return { status: registered ? "registered" : "failed" };
});

// 自动更新状态占位（spec 3.15：placeholder，不引入 electron-updater，不默认启用）
// 后续接入真实 auto-update 时替换此 handler 即可，前端 adapter 接口不变。
ipcMain.handle("chriptmas:auto-update-status", () => ({
  status: "unavailable",
  reason: "auto-update is not configured",
  enabled: false,
}));

// ── 桌面宠物 IPC ──

// 宠物被点击 → 打开/聚焦主窗口
ipcMain.handle("chriptmas:open-main-window", () => {
  showMainWindow();
  return { status: "shown" };
});

// 获取宠物 mood（当前返回默认值，后续可扩展为根据 activity 推断）
ipcMain.handle("chriptmas:pet-mood", () => {
  // 后续扩展入口：
  // - 可读取 activity-overview API 推断 mood
  // - 可根据是否有 running job 返回 "analyzing"
  // - 可根据今日是否有新记忆返回 "celebrating"
  return {
    mood: "calm",
    label: "安静偏亮",
  };
});

// 隐藏宠物（从托盘菜单或主窗口触发）
ipcMain.handle("chriptmas:pet-hide", () => {
  if (petWindow !== null && petWindow.isVisible()) {
    petWindow.hide();
    return { status: "hidden" };
  }
  return { status: "already-hidden" };
});

function localFileFilters(mediaKind) {
  if (mediaKind === "image") {
    return [{ name: "Images", extensions: ["png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"] }];
  }
  if (mediaKind === "audio") {
    return [{ name: "Audio", extensions: ["wav", "mp3", "m4a", "aac", "flac", "ogg"] }];
  }
  if (mediaKind === "video") {
    return [{ name: "Video", extensions: ["mp4", "mov", "mkv", "avi", "webm", "m4v"] }];
  }
  if (mediaKind === "document") {
    return [{ name: "Documents", extensions: ["pdf", "doc", "docx"] }];
  }
  return [{ name: "All Files", extensions: ["*"] }];
}

function safeShortText(value, fallback) {
  if (typeof value !== "string") {
    return fallback;
  }
  return value.replace(/\s+/g, " ").trim().slice(0, 180) || fallback;
}

app.whenReady().then(async () => {
  try {
    await startSidecar();
  } catch (error) {
    dialog.showErrorBox("Chriptmas OS 启动失败", "本地后端身份验证失败。请检查运行时后重新启动。\n\n错误：" + String(error.message || error));
    app.quit();
    return;
  }
  createMainWindow();
  createPetWindow();
  createTray();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createMainWindow();
      createPetWindow();
    }
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});

app.on("before-quit", () => {
  app.isQuitting = true;
  void sidecarSupervisor?.stop();
});

app.on("will-quit", () => {
  globalShortcut.unregisterAll();
});
