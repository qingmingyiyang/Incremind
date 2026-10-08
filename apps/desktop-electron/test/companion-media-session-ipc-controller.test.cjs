const assert = require("node:assert/strict");
const test = require("node:test");

const {
  CHANNELS,
  READY_CHANNEL,
  PROJECTION_CHANNEL,
  CompanionMediaSessionIpcController,
} = require("../src/companion/media-session-ipc-controller.cjs");

function fixture(overrides = {}) {
  const handlers = new Map();
  const listeners = new Map();
  const removedHandlers = [];
  const removedListeners = [];
  const calls = [];
  const projection = Object.freeze({ status: "playing", title: "", artist: "", commentary: "" });
  const mediaController = {
    refreshConfig: async () => { calls.push(["refresh"]); return { config: { enabled: true } }; },
    current: () => { calls.push(["current"]); return projection; },
  };
  const mainSender = { id: 11, send: (channel, payload) => calls.push(["main-send", channel, payload]) };
  const petSender = { id: 22, send: (channel, payload) => calls.push(["pet-send", channel, payload]) };
  const mainWindow = { isDestroyed: () => false, webContents: { id: 11 } };
  const petWindow = { isDestroyed: () => false, webContents: { id: 22 } };
  const options = {
    ipcMain: {
      handle: (channel, handler) => handlers.set(channel, handler),
      removeHandler: (channel) => { removedHandlers.push(channel); handlers.delete(channel); },
      on: (channel, listener) => listeners.set(channel, listener),
      removeListener: (channel, listener) => {
        removedListeners.push([channel, listener]);
        if (listeners.get(channel) === listener) listeners.delete(channel);
      },
    },
    requireMainRenderer: () => calls.push(["guard"]),
    mediaController,
    mainWindowProvider: () => mainWindow,
    petWindowProvider: () => petWindow,
    ...overrides,
  };
  return {
    controller: new CompanionMediaSessionIpcController(options), handlers, listeners,
    removedHandlers, removedListeners, calls, projection, mediaController,
    mainSender, petSender, mainWindow, petWindow,
  };
}

test("installs and disposes the complete media-session IPC surface", () => {
  const value = fixture();
  assert.equal(value.controller.install(), true);
  assert.deepEqual([...value.handlers.keys()], CHANNELS);
  assert.deepEqual([...value.listeners.keys()], [READY_CHANNEL]);
  assert.equal(value.controller.install(), false);
  assert.equal(value.controller.dispose(), true);
  assert.deepEqual(value.removedHandlers, CHANNELS);
  assert.equal(value.removedListeners.length, 1);
  assert.equal(value.removedListeners[0][0], READY_CHANNEL);
  assert.equal(value.controller.dispose(), false);
});

test("media invokes validate the main renderer before runtime access", async () => {
  const calls = [];
  const value = fixture({
    requireMainRenderer: () => { calls.push("guard"); throw new Error("ipc_main_sender_rejected"); },
    mediaController: {
      refreshConfig: () => calls.push("refresh"),
      current: () => calls.push("current"),
    },
  });
  value.controller.install();
  for (const channel of CHANNELS) {
    await assert.rejects(Promise.resolve().then(() => value.handlers.get(channel)({})), /ipc_main_sender_rejected/);
  }
  assert.deepEqual(calls, ["guard", "guard"]);
});

test("refresh and status delegate to the existing Media runtime", async () => {
  const value = fixture();
  value.controller.install();
  assert.deepEqual(await value.handlers.get(CHANNELS[0])({}), { config: { enabled: true } });
  assert.equal(value.handlers.get(CHANNELS[1])({}), value.projection);
  assert.deepEqual(value.calls, [["guard"], ["refresh"], ["guard"], ["current"]]);
});

test("ready routes the current projection only to the requesting main or pet renderer", () => {
  const value = fixture();
  value.controller.install();
  const ready = value.listeners.get(READY_CHANNEL);
  assert.equal(ready({ sender: value.mainSender }), true);
  assert.deepEqual(value.calls, [["current"], ["main-send", PROJECTION_CHANNEL, value.projection]]);
  value.calls.length = 0;
  assert.equal(ready({ sender: value.petSender }), true);
  assert.deepEqual(value.calls, [["current"], ["pet-send", PROJECTION_CHANNEL, value.projection]]);
});

test("ready silently ignores unknown and destroyed renderer authorities", () => {
  const value = fixture();
  value.controller.install();
  const ready = value.listeners.get(READY_CHANNEL);
  assert.equal(ready({ sender: { id: 33, send: () => value.calls.push(["unexpected"]) } }), false);
  assert.deepEqual(value.calls, []);

  const destroyed = fixture({
    mainWindowProvider: () => ({ isDestroyed: () => true, webContents: { id: 11 } }),
    petWindowProvider: () => ({ isDestroyed: () => true, webContents: { id: 22 } }),
  });
  destroyed.controller.install();
  assert.equal(destroyed.listeners.get(READY_CHANNEL)({ sender: destroyed.mainSender }), false);
  assert.deepEqual(destroyed.calls, []);
});

test("failed ready registration rolls back all Media handlers", () => {
  const removedHandlers = [];
  const removedListeners = [];
  const value = fixture({
    ipcMain: {
      handle: () => {},
      removeHandler: (channel) => removedHandlers.push(channel),
      on: () => { throw new Error("registration_failed"); },
      removeListener: (channel) => removedListeners.push(channel),
    },
  });
  assert.throws(() => value.controller.install(), /registration_failed/);
  assert.deepEqual(removedHandlers, CHANNELS);
  assert.deepEqual(removedListeners, [READY_CHANNEL]);
  assert.equal(value.controller.installed, false);
});
