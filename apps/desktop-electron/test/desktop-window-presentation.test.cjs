const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const test = require("node:test");

const {
  MAIN_WINDOW_TITLE_BAR,
  MAIN_WINDOW_MINIMUM,
  PRODUCT_NAME,
  configureMainWindowPresentation,
  ensureMainWindowBounds,
} = require("../src/desktop-window-presentation.cjs");

class WindowMock extends EventEmitter {
  constructor(bounds = { x: 10, y: 20, width: 340, height: 440 }) {
    super();
    this.bounds = bounds;
    this.calls = [];
  }
  setTitle(value) { this.calls.push(["title", value]); }
  setAutoHideMenuBar(value) { this.calls.push(["auto-hide-menu", value]); }
  setMenuBarVisibility(value) { this.calls.push(["menu-visible", value]); }
  removeMenu() { this.calls.push(["remove-menu"]); }
  setTitleBarOverlay(value) { this.calls.push(["title-bar-overlay", value]); }
  setMinimumSize(width, height) { this.calls.push(["minimum", width, height]); }
  getBounds() { return { ...this.bounds }; }
  setBounds(bounds) { this.bounds = bounds; this.calls.push(["bounds", bounds]); }
}

test("native caption buttons share the renderer title-row surface without an isolated color block", () => {
  assert.deepEqual(MAIN_WINDOW_TITLE_BAR, {
    light: { color: "#00000000", symbolColor: "#151B28", height: 40 },
    dark: { color: "#00000000", symbolColor: "#F4F7FB", height: 40 },
  });
});

test("main window chrome uses the product title and removes the default menu", () => {
  const window = new WindowMock();
  configureMainWindowPresentation(window);
  assert.deepEqual(window.calls.slice(0, 4), [
    ["title", PRODUCT_NAME],
    ["auto-hide-menu", true],
    ["menu-visible", false],
    ["remove-menu"],
  ]);
  let prevented = false;
  window.emit("page-title-updated", { preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.deepEqual(window.calls.at(-1), ["title", PRODUCT_NAME]);
});

test("main window chrome follows the native light and dark appearance and releases its listener", () => {
  const window = new WindowMock();
  const nativeTheme = new EventEmitter();
  nativeTheme.shouldUseDarkColors = false;

  configureMainWindowPresentation(window, { nativeTheme });
  assert.deepEqual(window.calls.find(([name]) => name === "title-bar-overlay"), [
    "title-bar-overlay",
    MAIN_WINDOW_TITLE_BAR.light,
  ]);

  nativeTheme.shouldUseDarkColors = true;
  nativeTheme.emit("updated");
  assert.deepEqual(window.calls.at(-1), ["title-bar-overlay", MAIN_WINDOW_TITLE_BAR.dark]);

  window.emit("closed");
  const callCount = window.calls.length;
  nativeTheme.shouldUseDarkColors = false;
  nativeTheme.emit("updated");
  assert.equal(window.calls.length, callCount);
});

test("undersized main window is restored without changing its position", () => {
  const window = new WindowMock();
  ensureMainWindowBounds(window);
  assert.deepEqual(window.calls[0], ["minimum", MAIN_WINDOW_MINIMUM.width, MAIN_WINDOW_MINIMUM.height]);
  assert.deepEqual(window.bounds, { x: 10, y: 20, width: 960, height: 680 });
});

test("valid main window bounds remain unchanged", () => {
  const window = new WindowMock({ x: 10, y: 20, width: 1180, height: 820 });
  ensureMainWindowBounds(window, { x: 0, y: 0, width: 1440, height: 900 });
  assert.equal(window.calls.some(([name]) => name === "bounds"), false);
});

test("off-screen main window is restored into the nearest display work area", () => {
  const window = new WindowMock({ x: 5000, y: 5000, width: 1180, height: 820 });
  ensureMainWindowBounds(window, { x: 0, y: 0, width: 1440, height: 860 });
  assert.deepEqual(window.bounds, { x: 260, y: 40, width: 1180, height: 820 });
});

test("negative-coordinate display remains a valid restoration target", () => {
  const window = new WindowMock({ x: -2500, y: -300, width: 1180, height: 820 });
  ensureMainWindowBounds(window, { x: -1920, y: 0, width: 1920, height: 1040 });
  assert.deepEqual(window.bounds, { x: -1920, y: 0, width: 1180, height: 820 });
});
