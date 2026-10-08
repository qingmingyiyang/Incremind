const assert = require("node:assert/strict");
const test = require("node:test");

const { DesktopSurfaceCoordinator } = require("../src/desktop-surface-coordinator.cjs");

function windowStub({ visible = false, loading = false, minimized = false, bounds = { x: 10, y: 20, width: 300, height: 200 } } = {}) {
  const calls = [];
  return {
    calls,
    webContents: { isLoadingMainFrame: () => loading },
    isDestroyed: () => false,
    isVisible: () => visible,
    isMinimized: () => minimized,
    getBounds: () => ({ ...bounds }),
    hide: () => calls.push("hide"),
    show: () => calls.push("show"),
    showInactive: () => calls.push("showInactive"),
    focus: () => calls.push("focus"),
    restore: () => calls.push("restore"),
    setIgnoreMouseEvents: (...args) => calls.push(["setIgnoreMouseEvents", ...args]),
    setBounds: (...args) => calls.push(["setBounds", ...args]),
  };
}

function fixture(overrides = {}) {
  let mainWindow = Object.hasOwn(overrides, "mainWindow") ? overrides.mainWindow : windowStub();
  let petWindow = Object.hasOwn(overrides, "petWindow") ? overrides.petWindow : windowStub();
  let overlayWindow = Object.hasOwn(overrides, "overlayWindow") ? overrides.overlayWindow : windowStub();
  let desktopReady = overrides.desktopReady ?? true;
  const events = [];
  const coordinator = new DesktopSurfaceCoordinator({
    mainWindowProvider: () => mainWindow,
    petWindowProvider: () => petWindow,
    overlayWindowProvider: () => overlayWindow,
    createMainWindow: () => (mainWindow = windowStub()),
    createPetWindow: () => (petWindow = windowStub()),
    createOverlayWindow: () => (overlayWindow = windowStub({ loading: true })),
    desktopReadyProvider: () => desktopReady,
    screen: { getDisplayMatching: (bounds) => ({ workArea: { x: -100, y: 0, width: 900, height: 700 }, bounds }) },
    ensureMainWindowBounds: (window, workArea) => events.push(["ensure", window, workArea]),
    resolveOverlayBounds: (input) => ({ x: input.petBounds.x + 5, y: input.workArea.y + 7, width: 240, height: 120 }),
    savePetWindowPosition: () => events.push("save"),
    cancelActiveGesture: () => events.push("cancel"),
    pollCompanionState: () => events.push("poll"),
  });
  return {
    coordinator,
    events,
    get mainWindow() { return mainWindow; },
    get petWindow() { return petWindow; },
    get overlayWindow() { return overlayWindow; },
    setDesktopReady(value) { desktopReady = value; },
  };
}

test("main intent survives pre-ready startup and presentation restores safe bounds", () => {
  const state = fixture({ desktopReady: false, mainWindow: null });
  state.coordinator.showMainWindow();
  assert.equal(state.coordinator.wants("main"), true);
  assert.equal(state.mainWindow, null);

  state.setDesktopReady(true);
  state.coordinator.showMainWindow();
  assert.deepEqual(state.mainWindow.calls, ["show", "focus"]);
  assert.equal(state.events[0][0], "ensure");
  assert.deepEqual(state.events[0][2], { x: -100, y: 0, width: 900, height: 700 });
});

test("presenting a minimized main window restores it before show and focus", () => {
  const mainWindow = windowStub({ minimized: true });
  const state = fixture({ mainWindow });
  assert.equal(state.coordinator.presentMainWindow(), true);
  assert.deepEqual(mainWindow.calls, ["restore", "show", "focus"]);
});

test("main and pet surfaces remain mutually exclusive", () => {
  const mainWindow = windowStub({ visible: true });
  const petWindow = windowStub({ visible: true });
  const state = fixture({ mainWindow, petWindow });

  state.coordinator.showPetWindow();
  assert.equal(state.coordinator.wants("pet"), true);
  assert.deepEqual(mainWindow.calls, ["hide"]);
  assert.deepEqual(petWindow.calls, [["setIgnoreMouseEvents", true, { forward: true }], "show", "focus"]);
  assert.deepEqual(state.events, ["poll"]);

  state.coordinator.showMainWindow();
  assert.equal(state.coordinator.wants("main"), true);
  assert.equal(petWindow.calls.at(-1), "hide");
  assert.deepEqual(mainWindow.calls.slice(-2), ["show", "focus"]);
});

test("hidden surface saves position, cancels gestures and clears overlay request", () => {
  const petWindow = windowStub({ visible: true });
  const overlayWindow = windowStub({ visible: true });
  const state = fixture({ petWindow, overlayWindow });
  state.coordinator.showOverlay({ focus: false });

  assert.deepEqual(state.coordinator.hidePetSurfaces(), { status: "hidden" });
  assert.equal(state.coordinator.wants("hidden"), true);
  assert.deepEqual(state.events.slice(-2), ["save", "cancel"]);
  assert.equal(petWindow.calls.at(-1), "hide");
  assert.equal(overlayWindow.calls.at(-1), "hide");
  assert.equal(state.coordinator.overlayRequested, false);
});

test("non-focus overlay intent survives first-window loading and ready replay", () => {
  const petWindow = windowStub({ visible: true });
  const state = fixture({ petWindow, overlayWindow: null });

  state.coordinator.showOverlay({ focus: false });
  const overlayWindow = state.overlayWindow;
  assert.equal(state.coordinator.overlayRequested, true);
  assert.equal(state.coordinator.overlayFocus, false);
  assert.doesNotMatch(overlayWindow.calls.join(" "), /showInactive|focus/);

  assert.equal(state.coordinator.onOverlayReady(overlayWindow), true);
  assert.equal(overlayWindow.calls.at(-1), "showInactive");
  assert.equal(overlayWindow.calls.includes("show"), false);
  assert.equal(overlayWindow.calls.includes("focus"), false);
});

test("latest overlay focus intent wins and positioning uses the pet display work area", () => {
  const petWindow = windowStub({ visible: true, bounds: { x: 400, y: 30, width: 180, height: 220 } });
  const overlayWindow = windowStub({ loading: true });
  const state = fixture({ petWindow, overlayWindow });

  state.coordinator.showOverlay({ focus: false });
  state.coordinator.showOverlay({ focus: true });
  assert.equal(state.coordinator.onOverlayReady(overlayWindow), true);
  assert.deepEqual(overlayWindow.calls.slice(-3), [
    ["setBounds", { x: 405, y: 7, width: 240, height: 120 }, false],
    "show",
    "focus",
  ]);
});

test("ready callbacks reject stale windows and overlay close clears pending intent", () => {
  const state = fixture({ petWindow: windowStub({ visible: true }) });
  const stale = windowStub();
  state.coordinator.showOverlay({ focus: false });
  assert.equal(state.coordinator.onMainReady(stale), false);
  assert.equal(state.coordinator.onPetReady(stale), false);
  assert.equal(state.coordinator.onOverlayReady(stale), false);

  state.coordinator.onOverlayClosed();
  assert.equal(state.coordinator.overlayRequested, false);
  assert.equal(state.coordinator.overlayFocus, true);
});
