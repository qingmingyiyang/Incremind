const assert = require("node:assert/strict");
const test = require("node:test");

const { EVENTS, CompanionProjectionReadyIpcController } = require("../src/companion/projection-ready-ipc-controller.cjs");

function fixture(overrides = {}) {
  const listeners = new Map();
  const removed = [];
  const calls = [];
  const mainFrame = { id: "pet-main-frame" };
  const webContents = { id: 42, mainFrame };
  const petWindow = { isDestroyed: () => false, webContents };
  const options = {
    ipcMain: {
      on: (channel, listener) => listeners.set(channel, listener),
      removeListener: (channel, listener) => {
        removed.push([channel, listener]);
        if (listeners.get(channel) === listener) listeners.delete(channel);
      },
    },
    petWindowProvider: () => petWindow,
    stateController: { deliverCurrent: () => calls.push("state") },
    appearanceArbiter: { deliverCurrent: () => calls.push("arbiter") },
    appearanceRuntimeController: { deliverCurrent: () => calls.push("appearance") },
    ...overrides,
  };
  return { controller: new CompanionProjectionReadyIpcController(options), listeners, removed, calls, petWindow, webContents, mainFrame };
}

test("installs and disposes the fixed projection-ready event surface", () => {
  const value = fixture();
  assert.equal(value.controller.install(), true);
  assert.deepEqual([...value.listeners.keys()], EVENTS);
  assert.equal(value.controller.install(), false);
  assert.equal(value.controller.dispose(), true);
  assert.deepEqual(value.removed.map(([channel]) => channel), EVENTS);
  assert.equal(value.controller.dispose(), false);
});

test("state-ready replays state and arbiter only for the current pet identity", () => {
  const value = fixture();
  value.controller.install();
  const ready = value.listeners.get(EVENTS[0]);
  assert.equal(ready({ sender: { id: 41 } }), false);
  assert.deepEqual(value.calls, []);
  assert.equal(ready({ sender: { id: 42 } }), true);
  assert.deepEqual(value.calls, ["state", "arbiter"]);
});

test("appearance-ready requires the exact current pet webContents and main frame", () => {
  const value = fixture();
  value.controller.install();
  const ready = value.listeners.get(EVENTS[1]);
  assert.equal(ready({ sender: { id: 42 }, senderFrame: value.mainFrame }), false);
  assert.equal(ready({ sender: value.webContents, senderFrame: { id: "subframe" } }), false);
  assert.deepEqual(value.calls, []);
  assert.equal(ready({ sender: value.webContents, senderFrame: value.mainFrame }), true);
  assert.deepEqual(value.calls, ["appearance"]);
});

test("destroyed or absent pet windows never replay a projection", () => {
  for (const petWindowProvider of [
    () => null,
    () => ({ isDestroyed: () => true, webContents: { id: 42, mainFrame: {} } }),
  ]) {
    const value = fixture({ petWindowProvider });
    value.controller.install();
    assert.equal(value.listeners.get(EVENTS[0])({ sender: { id: 42 } }), false);
    assert.equal(value.listeners.get(EVENTS[1])({ sender: value.webContents, senderFrame: value.mainFrame }), false);
    assert.deepEqual(value.calls, []);
  }
});

test("failed installation rolls back only projection listeners already registered", () => {
  const removed = [];
  let count = 0;
  const value = fixture({
    ipcMain: {
      on: () => { count += 1; if (count === 2) throw new Error("registration_failed"); },
      removeListener: (channel) => removed.push(channel),
    },
  });
  assert.throws(() => value.controller.install(), /registration_failed/);
  assert.deepEqual(removed, [EVENTS[0]]);
  assert.equal(value.controller.installed, false);
});
