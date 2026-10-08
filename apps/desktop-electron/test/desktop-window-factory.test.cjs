"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");

const { DesktopWindowFactory, OVERLAY_SESSION_PARTITION, PET_SESSION_PARTITION } = require("../src/desktop-window-factory.cjs");

function fixture() {
  const events = [];
  class BrowserWindow {
    constructor(options) {
      this.options = options;
      this.webContents = { session: { id: `session-${events.length}` } };
      events.push(["create", this]);
    }
    loadURL(value) { events.push(["loadURL", this, value]); }
    loadFile(value, options) { events.push(["loadFile", this, value, options]); }
    setBackgroundColor(value) { events.push(["background", this, value]); }
    setIgnoreMouseEvents(value, options) { events.push(["mouse", this, value, options]); }
    setAlwaysOnTop(...args) { events.push(["top", this, ...args]); }
  }
  const baseDir = path.resolve("C:\\chriptmas-electron-src");
  const factory = new DesktopWindowFactory({
    BrowserWindow,
    baseDir,
    developmentOriginProvider: () => "http://127.0.0.1:4173",
    installPetSessionPolicy: (...args) => events.push(["pet-policy", ...args]),
    installNavigationPolicy: (...args) => events.push(["navigation", ...args]),
    configureMainWindow: (...args) => events.push(["main-presentation", ...args]),
  });
  return { factory, events, baseDir };
}

test("main factory owns fixed presentation security navigation and URL loading", () => {
  const value = fixture();
  const prepared = value.factory.prepareMain({ entry: { type: "url", value: "http://127.0.0.1:4173/?view=home" }, backendOrigin: "http://127.0.0.1:3210" });
  const window = prepared.window;
  assert.equal(window.options.width, 1180);
  assert.equal(window.options.height, 820);
  assert.equal(window.options.minWidth, 960);
  assert.equal(window.options.minHeight, 680);
  assert.equal(window.options.title, "Chriptmas OS");
  assert.equal(window.options.show, false);
  assert.equal(window.options.titleBarStyle, "hidden");
  assert.deepEqual(window.options.titleBarOverlay, {
    color: "#00000000",
    symbolColor: "#151B28",
    height: 40,
  });
  assert.equal(window.options.backgroundColor, "#F3F5F9");
  assert.equal(window.options.webPreferences.preload, path.join(value.baseDir, "preload.cjs"));
  assert.deepEqual({
    contextIsolation: window.options.webPreferences.contextIsolation,
    nodeIntegration: window.options.webPreferences.nodeIntegration,
    sandbox: window.options.webPreferences.sandbox,
    webSecurity: window.options.webPreferences.webSecurity,
  }, { contextIsolation: true, nodeIntegration: false, sandbox: true, webSecurity: true });
  assert.deepEqual(value.events.slice(1), [
    ["main-presentation", window],
    ["navigation", window.webContents, { rendererOrigin: "http://127.0.0.1:4173", backendOrigin: "http://127.0.0.1:3210" }],
  ]);
  prepared.load();
  assert.deepEqual(value.events.at(-1),
    ["loadURL", window, "http://127.0.0.1:4173/?view=home"],
  );
  assert.throws(() => prepared.load(), /desktop_window_load_already_started/);
});

test("main file entry keeps the local hash and file renderer origin", () => {
  const value = fixture();
  const prepared = value.factory.prepareMain({ entry: { type: "file", value: "C:\\frontend\\index.html", hash: "view=home" }, backendOrigin: "http://127.0.0.1:3210" });
  const window = prepared.window;
  assert.deepEqual(value.events.at(-1), ["navigation", window.webContents, { rendererOrigin: "file://", backendOrigin: "http://127.0.0.1:3210" }]);
  prepared.load();
  assert.deepEqual(value.events.at(-1), ["loadFile", window, "C:\\frontend\\index.html", { hash: "view=home" }]);
});

test("pet factory owns its ephemeral partition preload and transparent native attributes", () => {
  const value = fixture();
  const prepared = value.factory.preparePet({ entry: { type: "file", value: "C:\\frontend\\index.html", hash: "view=pet" }, savedPosition: { x: -400, y: 80 } });
  const window = prepared.window;
  assert.equal(window.options.partition, undefined);
  assert.equal(window.options.webPreferences.partition, PET_SESSION_PARTITION);
  assert.equal(window.options.webPreferences.preload, path.join(value.baseDir, "pet-preload.cjs"));
  assert.equal(window.options.transparent, true);
  assert.equal(window.options.frame, false);
  assert.equal(window.options.x, -400);
  assert.equal(window.options.y, 80);
  assert.deepEqual(value.events.slice(1), [
    ["pet-policy", window.webContents.session, { developmentOrigin: "http://127.0.0.1:4173" }],
    ["navigation", window.webContents, { rendererOrigin: "file://" }],
    ["background", window, "#00000000"],
    ["mouse", window, true, { forward: true }],
  ]);
  prepared.load();
  assert.deepEqual(value.events.slice(-2), [
    ["loadFile", window, "C:\\frontend\\index.html", { hash: "view=pet" }],
    ["top", window, true, "floating", 1],
  ]);
});

test("overlay factory is fixed local isolated transparent and devtools-free", () => {
  const value = fixture();
  const prepared = value.factory.prepareOverlay();
  const window = prepared.window;
  assert.equal(window.options.width, 360);
  assert.equal(window.options.height, 180);
  assert.equal(window.options.webPreferences.partition, OVERLAY_SESSION_PARTITION);
  assert.equal(window.options.webPreferences.devTools, false);
  assert.equal(window.options.webPreferences.preload, path.join(value.baseDir, "companion-overlay-preload.cjs"));
  assert.deepEqual(value.events.slice(1), [
    ["pet-policy", window.webContents.session, { developmentOrigin: "" }],
    ["navigation", window.webContents, { rendererOrigin: "file://" }],
    ["background", window, "#00000000"],
  ]);
  prepared.load();
  assert.deepEqual(value.events.slice(-2), [
    ["top", window, true, "floating", 1],
    ["loadFile", window, path.join(value.baseDir, "companion", "overlay.html"), undefined],
  ]);
});

test("factory rejects invalid composition dependencies and entries", () => {
  assert.throws(() => new DesktopWindowFactory(), /desktop_window_factory_options_invalid/);
  const value = fixture();
  assert.throws(() => value.factory.prepareMain({ entry: { type: "other", value: "x" }, backendOrigin: "" }), /desktop_window_entry_invalid/);
  assert.throws(() => value.factory.prepareMain({ entry: { type: "file", value: "x", hash: "x".repeat(513) }, backendOrigin: "" }), /desktop_window_entry_invalid/);
  assert.throws(() => value.factory.prepareMain({ entry: { type: "file", value: "x" }, backendOrigin: null }), /desktop_window_backend_origin_invalid/);
});
