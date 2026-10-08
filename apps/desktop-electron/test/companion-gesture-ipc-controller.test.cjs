const assert = require("node:assert/strict");
const test = require("node:test");

const { CHANNELS, EVENT_METHODS, CompanionGestureIpcController } = require("../src/companion/gesture-ipc-controller.cjs");

function fixture(overrides = {}) {
  const listeners = new Map();
  const removed = [];
  const calls = [];
  const gestureController = Object.fromEntries(EVENT_METHODS.map(([, method]) => [
    method,
    (payload) => calls.push([method, payload]),
  ]));
  const petWindow = { isDestroyed: () => false, webContents: { id: 42 } };
  const options = {
    ipcMain: {
      on: (channel, listener) => listeners.set(channel, listener),
      removeListener: (channel, listener) => {
        removed.push([channel, listener]);
        if (listeners.get(channel) === listener) listeners.delete(channel);
      },
    },
    petWindowProvider: () => petWindow,
    gestureController,
    ...overrides,
  };
  return { controller: new CompanionGestureIpcController(options), listeners, removed, calls, gestureController, petWindow };
}

test("installs and disposes the fixed four-channel gesture allowlist", () => {
  const value = fixture();
  assert.equal(value.controller.install(), true);
  assert.deepEqual([...value.listeners.keys()], CHANNELS);
  assert.equal(value.controller.install(), false);
  assert.equal(value.controller.dispose(), true);
  assert.deepEqual(value.removed.map(([channel]) => channel), CHANNELS);
  assert.equal(value.controller.dispose(), false);
});

test("each channel delegates its payload identity to exactly one gesture method", () => {
  const value = fixture();
  value.controller.install();
  for (const [index, [channel, method]] of EVENT_METHODS.entries()) {
    const payload = { sequence: index };
    assert.equal(value.listeners.get(channel)({ sender: { id: 42 } }, payload), true);
    assert.deepEqual(value.calls.at(-1), [method, payload]);
  }
  assert.equal(value.calls.length, EVENT_METHODS.length);
});

test("unknown absent and destroyed pet authorities produce zero domain calls", () => {
  const value = fixture();
  value.controller.install();
  assert.equal(value.listeners.get(CHANNELS[0])({ sender: { id: 41 } }, {}), false);
  assert.deepEqual(value.calls, []);

  for (const petWindowProvider of [
    () => null,
    () => ({ isDestroyed: () => true, webContents: { id: 42 } }),
  ]) {
    const rejected = fixture({ petWindowProvider });
    rejected.controller.install();
    assert.equal(rejected.listeners.get(CHANNELS[0])({ sender: { id: 42 } }, {}), false);
    assert.deepEqual(rejected.calls, []);
  }
});

test("gesture domain rejection stays contained for one-way IPC", () => {
  const value = fixture({
    gestureController: {
      begin: () => { throw new Error("gesture_payload_invalid"); },
      move: () => {}, end: () => {}, click: () => {},
    },
  });
  value.controller.install();
  assert.doesNotThrow(() => {
    assert.equal(value.listeners.get(CHANNELS[0])({ sender: { id: 42 } }, { secret: "not-valid" }), false);
  });
});

test("failed installation rolls back gesture listeners already registered", () => {
  const removed = [];
  let count = 0;
  const value = fixture({
    ipcMain: {
      on: () => { count += 1; if (count === 4) throw new Error("registration_failed"); },
      removeListener: (channel) => removed.push(channel),
    },
  });
  assert.throws(() => value.controller.install(), /registration_failed/);
  assert.deepEqual(removed, CHANNELS.slice(0, 3));
  assert.equal(value.controller.installed, false);
});
