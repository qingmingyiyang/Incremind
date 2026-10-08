// themeBootstrap.js — 主题与性能模式的首帧应用模块
//
// 解决"默认白天主题闪烁"：在 React createRoot().render() 之前同步调用
// bootstrapThemeAndPerformance()，让首帧 HTML 就带上 data-theme /
// data-performance 属性，浏览器第一帧就用用户偏好的样式渲染。
//
// 设计要点：
// 1. 读取与校验分离（readStored*），便于 React useState 复用同一份默认值
// 2. 应用与持久化分离（apply*），给设置页切换时复用
// 3. bootstrap 只读 + 写 data 属性，不写 localStorage，避免无谓写入
// 4. system 模式：存储 "system"，但实际写入 documentElement 的是解析后的 light/dark，
//    并监听 prefers-color-scheme 变化自动切换

const THEME_KEY = "chriptmas-os-theme";
const PERFORMANCE_KEY = "chriptmas-os-performance";

const VALID_THEMES = ["light", "dark", "system"];
const VALID_PERFORMANCES = ["balanced", "low"];

export const DEFAULT_THEME = "system";
export const DEFAULT_PERFORMANCE = "balanced";

// system 模式监听器（模块级单例，避免重复绑定）
let systemThemeMedia = null;
let systemThemeHandler = null;

function syncDesktopAppearance(mode, bridge = globalThis.electronAPI, scheduleAppearance = null) {
  if (typeof bridge?.setWindowAppearance !== "function") return;
  const apply = () => {
    try {
      Promise.resolve(bridge.setWindowAppearance(mode)).catch(() => {});
    } catch {
      // 浏览器预览或桌面桥接不可用时，页面主题仍独立生效。
    }
  };
  if (typeof scheduleAppearance === "function") scheduleAppearance(apply);
  else apply();
}

function scheduleAfterRendererPaint(callback) {
  if (typeof globalThis.requestAnimationFrame === "function") {
    globalThis.requestAnimationFrame(() => globalThis.requestAnimationFrame(callback));
    return;
  }
  callback();
}

/**
 * 读取系统当前颜色偏好。
 */
function readSystemTheme() {
  if (typeof window === "undefined" || !window.matchMedia) return "light";
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/**
 * 读取并校验 localStorage 中的主题值。
 * 非法或缺失时返回 DEFAULT_THEME，不写回 localStorage。
 */
export function readStoredTheme(storage = localStorage) {
  const raw = storage.getItem(THEME_KEY);
  return VALID_THEMES.includes(raw) ? raw : DEFAULT_THEME;
}

/**
 * 读取并校验 localStorage 中的性能模式值。
 * 非法或缺失时返回 DEFAULT_PERFORMANCE，不写回 localStorage。
 */
export function readStoredPerformance(storage = localStorage) {
  const raw = storage.getItem(PERFORMANCE_KEY);
  return VALID_PERFORMANCES.includes(raw) ? raw : DEFAULT_PERFORMANCE;
}

/**
 * 绑定 system 模式监听器：系统主题变化时自动更新 documentElement。
 * 仅在 mode === "system" 时生效；切换到显式 light/dark 时解绑。
 */
function bindSystemThemeListener(mode, { storage = localStorage, root = document.documentElement } = {}) {
  // 先解绑旧监听器
  if (systemThemeMedia && systemThemeHandler) {
    systemThemeMedia.removeEventListener("change", systemThemeHandler);
    systemThemeMedia = null;
    systemThemeHandler = null;
  }
  if (mode !== "system") return;
  if (typeof window === "undefined" || !window.matchMedia) return;
  systemThemeMedia = window.matchMedia("(prefers-color-scheme: dark)");
  systemThemeHandler = () => {
    const resolved = readSystemTheme();
    root.dataset.theme = resolved;
  };
  systemThemeMedia.addEventListener("change", systemThemeHandler);
}

/**
 * 应用主题到 documentElement 并持久化到 localStorage。
 * system 模式会解析为实际 light/dark 写入 data-theme，并监听系统变化。
 * 供设置页切换主题时调用。返回用户选择的 mode（含 "system"）。
 */
export function applyTheme(mode, {
  storage = localStorage,
  root = document.documentElement,
  bridge = globalThis.electronAPI,
  scheduleAppearance = scheduleAfterRendererPaint,
} = {}) {
  const next = VALID_THEMES.includes(mode) ? mode : DEFAULT_THEME;
  // 持久化用户选择（含 "system"）
  storage.setItem(THEME_KEY, next);
  // 实际写入解析后的 light/dark
  const resolved = next === "system" ? readSystemTheme() : next;
  root.dataset.theme = resolved;
  // 绑定/解绑 system 监听器
  bindSystemThemeListener(next, { storage, root });
  syncDesktopAppearance(next, bridge, scheduleAppearance);
  return next;
}

/**
 * 应用性能模式到 documentElement 并持久化到 localStorage。
 * 供设置页切换性能模式时调用。
 */
export function applyPerformance(mode, { storage = localStorage, root = document.documentElement } = {}) {
  const next = VALID_PERFORMANCES.includes(mode) ? mode : DEFAULT_PERFORMANCE;
  root.dataset.performance = next;
  storage.setItem(PERFORMANCE_KEY, next);
  return next;
}

/**
 * 首帧应用：在 React render 之前同步调用。
 * 只读 localStorage + 设置 data 属性，不写 localStorage，避免无谓写入。
 * system 模式会解析为实际 light/dark 并绑定监听器。
 * 返回 { theme, performance } 供调用方记录日志或调试。
 */
export function bootstrapThemeAndPerformance({ storage = localStorage, root = document.documentElement, bridge = globalThis.electronAPI } = {}) {
  const theme = readStoredTheme(storage);
  const performance = readStoredPerformance(storage);
  const resolved = theme === "system" ? readSystemTheme() : theme;
  root.dataset.theme = resolved;
  root.dataset.performance = performance;
  bindSystemThemeListener(theme, { storage, root });
  syncDesktopAppearance(theme, bridge);
  return { theme, performance };
}
