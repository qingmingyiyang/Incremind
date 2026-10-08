const assert = require("node:assert/strict");
const test = require("node:test");

const {
  CHANNELS,
  READY_CHANNEL,
  TERMS_URL,
  CompanionWeatherIpcController,
} = require("../src/companion/weather-ipc-controller.cjs");

function fixture(overrides = {}) {
  const handlers = new Map();
  const listeners = new Map();
  const removedHandlers = [];
  const removedListeners = [];
  const calls = [];
  const projection = Object.freeze({ condition: "rain", stale: false, revision: 4 });
  const weatherController = {
    refresh: async () => { calls.push(["refresh"]); return { config: { enabled: true } }; },
    current: () => { calls.push(["current"]); return projection; },
  };
  const webContents = {
    id: 42,
    send: (channel, payload) => calls.push(["send", channel, payload]),
  };
  const petWindow = { isDestroyed: () => false, webContents };
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
    weatherController,
    petWindowProvider: () => petWindow,
    openExternal: async (url) => calls.push(["open", url]),
    ...overrides,
  };
  return {
    controller: new CompanionWeatherIpcController(options), handlers, listeners,
    removedHandlers, removedListeners, calls, projection, weatherController, petWindow,
  };
}

test("installs and disposes the complete weather IPC surface", () => {
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

test("weather invokes validate the main renderer before runtime or Shell", async () => {
  const calls = [];
  const value = fixture({
    requireMainRenderer: () => { calls.push("guard"); throw new Error("ipc_main_sender_rejected"); },
    weatherController: {
      refresh: () => calls.push("refresh"),
      current: () => calls.push("current"),
    },
    openExternal: () => calls.push("open"),
  });
  value.controller.install();
  for (const channel of CHANNELS) {
    await assert.rejects(Promise.resolve().then(() => value.handlers.get(channel)({})), /ipc_main_sender_rejected/);
  }
  assert.deepEqual(calls, ["guard", "guard"]);
});

test("refresh delegates to the existing Weather runtime", async () => {
  const value = fixture();
  value.controller.install();
  assert.deepEqual(await value.handlers.get(CHANNELS[0])({}), { config: { enabled: true } });
  assert.deepEqual(value.calls, [["guard"], ["refresh"]]);
});

test("terms opening owns the fixed HTTPS authority", async () => {
  const value = fixture();
  value.controller.install();
  assert.deepEqual(await value.handlers.get(CHANNELS[1])({}), { status: "opened" });
  assert.deepEqual(value.calls, [["guard"], ["open", TERMS_URL]]);
});

test("ready projects current Weather only to the current pet sender", () => {
  const value = fixture();
  value.controller.install();
  const ready = value.listeners.get(READY_CHANNEL);
  assert.equal(ready({ sender: { id: 41 } }), false);
  assert.deepEqual(value.calls, []);
  assert.equal(ready({ sender: { id: 42 } }), true);
  assert.deepEqual(value.calls, [
    ["current"],
    ["send", "chriptmas:companion-weather", value.projection],
  ]);

  const destroyed = fixture({ petWindowProvider: () => ({ isDestroyed: () => true, webContents: { id: 42 } }) });
  destroyed.controller.install();
  assert.equal(destroyed.listeners.get(READY_CHANNEL)({ sender: { id: 42 } }), false);
  assert.deepEqual(destroyed.calls, []);
});

test("failed ready registration rolls back all Weather handlers", () => {
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
