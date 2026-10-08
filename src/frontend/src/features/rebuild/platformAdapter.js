// platformAdapter.js — 统一平台适配层
// selectLocalFile / registerShortcut）包装为带 browser fallback 的统一接口，
// 让前端组件不必各自判断运行环境。
//
// 设计原则：
// - Electron 环境优先走 electronAPI（原生体验）
// - 浏览器环境 fallback 到 Web API（Notification API、<input type="file">）
// - 不支持时返回明确的 status，让调用方能分支处理
// - 不做后端 HTTP 调用——openFolder 走 electronAPI.openPath，
//   仅当 electronAPI 不可用时才 fallback 到后端端点（由调用方决定）

const ELECTRON_API = () => globalThis.electronAPI;

// ── 环境检测 ──

export function getShellStatus() {
  const api = ELECTRON_API();
  if (api?.shell === "electron") return "electron";

  return "browser";
}

export function isElectron() {
  return getShellStatus() === "electron";
}



// 平台请求复用既有 JSON 传输约定。


// ── 打开文件夹 ──
// Electron: 走 electronAPI.openPath（原生 shell.openPath）
// Browser: 不支持，返回 unsupported（调用方可 fallback 到后端 HTTP 端点）

/**
 * 打开本地文件夹。
 * @param {string} folderPath 绝对路径
 * @returns {Promise<{status: "opened"|"unsupported"|"rejected", reason?: string}>}
 */
export async function openFolder(folderPath) {
  if (typeof folderPath !== "string" || !folderPath.trim()) {
    return { status: "rejected", reason: "path is required" };
  }
  const api = ELECTRON_API();
  if (typeof api?.openPath === "function") {
    try {
      const result = await api.openPath(folderPath);
      if (result?.status === "opened") return { status: "opened" };
      return {
        status: result?.status || "rejected",
        reason: result?.reason || "openPath returned non-opened status",
      };
    } catch (error) {
      return { status: "rejected", reason: error?.message || "openPath failed" };
    }
  }

  return { status: "unsupported", reason: "browser cannot open folder directly" };
}

// ── 通知 ──
// Electron: 走 electronAPI.showNotification（原生 Notification）
// Browser: fallback 到 Web Notification API（需用户授权）

let browserNotificationPermission = "default";

/**
 * 显示桌面通知。
 * @param {object} options {title, body}
 * @returns {Promise<{status: "shown"|"unsupported"|"denied"|"failed", reason?: string}>}
 */
export async function notify({ title, body } = {}) {
  const safeTitle = typeof title === "string" ? title : "Chriptmas OS";
  const safeBody = typeof body === "string" ? body : "";
  const api = ELECTRON_API();
  if (typeof api?.showNotification === "function") {
    try {
      const result = await api.showNotification({ title: safeTitle, body: safeBody });
      return { status: result?.status || "shown" };
    } catch (error) {
      return { status: "failed", reason: error?.message || "showNotification failed" };
    }
  }

  // Browser fallback: Web Notification API
  if (typeof globalThis.Notification === "undefined") {
    return { status: "unsupported", reason: "Notification API not available" };
  }
  if (browserNotificationPermission === "default") {
    browserNotificationPermission = await globalThis.Notification.requestPermission();
  }
  if (browserNotificationPermission !== "granted") {
    return { status: "denied", reason: "notification permission not granted" };
  }
  try {
    new globalThis.Notification(safeTitle, { body: safeBody });
    return { status: "shown" };
  } catch (error) {
    return { status: "failed", reason: error?.message || "Notification failed" };
  }
}

// ── 选择本地文件 ──
// Electron: 走 electronAPI.selectLocalFile（原生 dialog）
// Browser: 不支持返回路径，返回 unsupported
//   注意：浏览器环境应直接用 <input type="file">，不由此函数处理。

/**
 * 选择本地文件，返回文件路径（仅 Electron 环境）。
 * @param {object} options {mediaKind: "file"|"image"|"audio"|"video"|"document"}
 * @returns {Promise<{status: "selected"|"cancelled"|"unsupported", path?: string|null}>}
 */
export async function selectLocalFile({ mediaKind = "file" } = {}) {
  const api = ELECTRON_API();
  if (typeof api?.selectLocalFile !== "function") {
    return { status: "unsupported" };
  }
  try {
    const result = await api.selectLocalFile({ mediaKind });
    if (result === null || result === undefined) {
      return { status: "cancelled" };
    }
    // 兼容旧版返回数组的情况
    const filePath = Array.isArray(result) ? result[0] : result;
    if (!filePath) {
      return { status: "cancelled" };
    }
    return { status: "selected", path: filePath };
  } catch (error) {
    return { status: "cancelled", reason: error?.message };
  }
}

// ── 平台信息 ──

export async function getPlatformInfo() {
  const api = ELECTRON_API();
  if (typeof api?.getPlatformInfo === "function") {
    return api.getPlatformInfo();
  }
  return {
    appDataDir: null,
    pathSeparator: "/",
    platform: typeof navigator !== "undefined" && navigator.platform
      ? navigator.platform
      : "unknown",
    shell: getShellStatus(),
    version: null,
  };
}

// ── 全局快捷键 ──
// Electron: 走 electronAPI.registerShortcut（原生 globalShortcut）
// Browser: 不支持，返回 unsupported

/**
 * 注册全局快捷键（仅 Electron 环境）。
 * @param {string} accelerator Electron accelerator 字符串，如 "Ctrl+Shift+Alt+R"
 * @returns {Promise<{status: "registered"|"rejected"|"failed"|"unsupported", reason?: string}>}
 */
export async function registerShortcut(accelerator) {
  if (typeof accelerator !== "string" || !accelerator.trim()) {
    return { status: "rejected", reason: "accelerator is required" };
  }
  const api = ELECTRON_API();
  if (typeof api?.registerShortcut === "function") {
    try {
      const result = await api.registerShortcut(accelerator);
      return { status: result?.status || "failed", reason: result?.reason };
    } catch (error) {
      return { status: "failed", reason: error?.message || "registerShortcut failed" };
    }
  }

  return { status: "unsupported", reason: "browser cannot register global shortcut" };
}

// ── 自动更新状态（placeholder）──
// spec 3.15：getAutoUpdateStatus 占位，不引入真实 auto-update。
// Electron 环境走 IPC 返回 unavailable；浏览器环境同样返回 unavailable。
// 后续接入 electron-updater 时只需替换 main.cjs 的 handler，adapter 接口不变。

/**
 * 查询自动更新状态（当前为 placeholder，始终返回 unavailable）。
 * @returns {Promise<{status: "unavailable"|"available"|"downloading"|"ready", enabled: boolean, reason?: string}>}
 */
export async function getAutoUpdateStatus() {
  const api = ELECTRON_API();
  if (typeof api?.getAutoUpdateStatus === "function") {
    try {
      const result = await api.getAutoUpdateStatus();
      return {
        status: result?.status || "unavailable",
        enabled: Boolean(result?.enabled),
        reason: result?.reason,
      };
    } catch (error) {
      return { status: "unavailable", enabled: false, reason: error?.message };
    }
  }
  return {
    status: "unavailable",
    enabled: false,
    reason: "auto-update is not supported in this environment",
  };
}

// 仅供测试使用：重置 browser notification permission 缓存
export function _resetBrowserNotificationPermission() {
  browserNotificationPermission = "default";
}
