const test = require("node:test");
const assert = require("node:assert/strict");

const {
  CHANNELS,
  CompanionPetWindowIpcController,
} = require("../src/companion/pet-window-ipc-controller.cjs");

function harness() {
  const handlers = new Map();
  const calls = [];
  const petWindow = {
    webContents: { id: 7 },
    isDestroyed: () => false,
    isVisible: () => true,
    setIgnoreMouseEvents: (...args) => calls.push(["passthrough", ...args]),
    getPosition: () => [100, 200],
    getBounds: () => ({ width: 180, height: 220 }),
    setPosition: (...args) => calls.push(["position", ...args]),
  };
  const overlayWindow = { isVisible: () => true };
  const motionController = {
    beginDrag: () => calls.push(["begin-drag"]),
    settle: (window) => { calls.push(["settle", window]); return { mode: "bottom" }; },
  };
  const controller = new CompanionPetWindowIpcController({
    ipcMain: {
      handle: (channel, handler) => handlers.set(channel, handler),
      removeHandler: (channel) => handlers.delete(channel),
    },
    requireMainRenderer: (event) => {
      if (event.sender.id !== 1) throw new Error("ipc_main_sender_rejected");
    },
    requireKnownRenderer: (event) => {
      if (![1, 7].includes(event.sender.id)) throw new Error("ipc_window_sender_rejected");
    },
    petWindowProvider: () => petWindow,
    overlayWindowProvider: () => overlayWindow,
    showPetWindow: () => calls.push(["show-pet"]),
    showMainWindow: () => calls.push(["show-main"]),
    hidePetSurfaces: () => { calls.push(["hide-pet"]); return { status: "hidden" }; },
    showPetContextMenu: () => { calls.push(["context-menu"]); return { status: "opened" }; },
    positionCompanionOverlay: () => calls.push(["position-overlay"]),
    clampPetWindowPosition: (...args) => { calls.push(["clamp", ...args]); return { x: 110, y: 190 }; },
    motionController,
  });
  return { calls, controller, handlers, overlayWindow, petWindow };
}

const mainEvent = { sender: { id: 1 } };
const petEvent = { sender: { id: 7 } };

test("installs and disposes the fixed pet window IPC allowlist", () => {
  const { controller, handlers } = harness();
  assert.equal(controller.install(), true);
  assert.deepEqual([...handlers.keys()], CHANNELS);
  assert.equal(controller.install(), false);
  assert.equal(controller.dispose(), true);
  assert.equal(handlers.size, 0);
  assert.equal(controller.dispose(), false);
});

test("separates main-only companion entry from known-renderer window actions", () => {
  const { calls, controller } = harness();
  assert.deepEqual(controller.enterCompanionMode(mainEvent), { status: "shown" });
  assert.throws(() => controller.enterCompanionMode(petEvent), /ipc_main_sender_rejected/);
  assert.deepEqual(controller.openMainWindow(petEvent), { status: "shown" });
  assert.deepEqual(controller.hidePet(mainEvent), { status: "hidden" });
  assert.throws(() => controller.openMainWindow({ sender: { id: 99 } }), /ipc_window_sender_rejected/);
  assert.deepEqual(calls, [["show-pet"], ["show-main"], ["hide-pet"]]);
});

test("keeps context menu and transparent hit testing pet-renderer-only", () => {
  const { calls, controller } = harness();
  assert.deepEqual(controller.openContextMenu(petEvent), { status: "opened" });
  assert.deepEqual(controller.setMousePassthrough(petEvent, true), { status: "passthrough" });
  assert.deepEqual(controller.setMousePassthrough(petEvent, false), { status: "interactive" });
  assert.throws(() => controller.openContextMenu(mainEvent), /requires the pet renderer/);
  assert.deepEqual(calls, [
    ["context-menu"],
    ["passthrough", true, { forward: true }],
    ["passthrough", false, { forward: true }],
  ]);
});

test("moves only by bounded finite deltas and repositions a visible overlay", () => {
  const { calls, controller } = harness();
  assert.deepEqual(controller.move(petEvent, { dx: 10, dy: -10 }), { status: "moved" });
  assert.deepEqual(calls, [
    ["begin-drag"],
    ["clamp", 110, 190, 180, 220],
    ["position", 110, 190, false],
    ["position-overlay"],
  ]);
  for (const delta of [{ dx: 81, dy: 0 }, { dx: 0, dy: -81 }, { dx: "x", dy: 0 }]) {
    assert.throws(() => controller.move(petEvent, delta), /bounded deltas/);
  }
  assert.throws(() => controller.move(mainEvent, { dx: 1, dy: 1 }), /requires the pet renderer/);
});

test("finishes movement through the motion controller", () => {
  const { calls, controller, petWindow } = harness();
  assert.deepEqual(controller.finishMove(petEvent), { status: "settling", mode: "bottom" });
  assert.deepEqual(calls, [["settle", petWindow]]);
});
