const PRODUCT_NAME = "Chriptmas OS";
const MAIN_WINDOW_DEFAULT = Object.freeze({ width: 1180, height: 820 });
const MAIN_WINDOW_MINIMUM = Object.freeze({ width: 960, height: 680 });
const MAIN_WINDOW_TITLE_BAR = Object.freeze({
  light: Object.freeze({ color: "#00000000", symbolColor: "#151B28", height: 40 }),
  dark: Object.freeze({ color: "#00000000", symbolColor: "#F4F7FB", height: 40 }),
});

function resolveMainWindowTitleBar(shouldUseDarkColors = false) {
  return shouldUseDarkColors ? MAIN_WINDOW_TITLE_BAR.dark : MAIN_WINDOW_TITLE_BAR.light;
}

function configureMainWindowPresentation(window, { nativeTheme = null } = {}) {
  if (!window) throw new TypeError("main_window_required");
  window.setTitle(PRODUCT_NAME);
  window.setAutoHideMenuBar?.(true);
  window.setMenuBarVisibility?.(false);
  window.removeMenu?.();
  const syncTitleBar = () => {
    window.setTitleBarOverlay?.(resolveMainWindowTitleBar(nativeTheme?.shouldUseDarkColors === true));
  };
  syncTitleBar();
  nativeTheme?.on?.("updated", syncTitleBar);
  window.once?.("closed", () => {
    if (typeof nativeTheme?.off === "function") nativeTheme.off("updated", syncTitleBar);
    else nativeTheme?.removeListener?.("updated", syncTitleBar);
  });
  window.on?.("page-title-updated", (event) => {
    event?.preventDefault?.();
    window.setTitle(PRODUCT_NAME);
  });
}

function ensureMainWindowBounds(window, workArea = null) {
  if (!window) throw new TypeError("main_window_required");
  window.setMinimumSize?.(MAIN_WINDOW_MINIMUM.width, MAIN_WINDOW_MINIMUM.height);
  const bounds = window.getBounds?.();
  if (!bounds) return;
  const width = Math.max(bounds.width, MAIN_WINDOW_MINIMUM.width);
  const height = Math.max(bounds.height, MAIN_WINDOW_MINIMUM.height);
  const target = { ...bounds, width, height };
  if (isValidWorkArea(workArea)) {
    target.x = clamp(bounds.x, workArea.x, workArea.x + Math.max(0, workArea.width - width));
    target.y = clamp(bounds.y, workArea.y, workArea.y + Math.max(0, workArea.height - height));
  }
  if (
    target.x !== bounds.x
    || target.y !== bounds.y
    || target.width !== bounds.width
    || target.height !== bounds.height
  ) {
    window.setBounds?.(target);
  }
}

function isValidWorkArea(value) {
  return value
    && Number.isFinite(value.x)
    && Number.isFinite(value.y)
    && Number.isFinite(value.width)
    && Number.isFinite(value.height)
    && value.width > 0
    && value.height > 0;
}

function clamp(value, minimum, maximum) {
  return Math.min(Math.max(value, minimum), maximum);
}

module.exports = {
  MAIN_WINDOW_DEFAULT,
  MAIN_WINDOW_MINIMUM,
  MAIN_WINDOW_TITLE_BAR,
  PRODUCT_NAME,
  configureMainWindowPresentation,
  ensureMainWindowBounds,
  resolveMainWindowTitleBar,
};
