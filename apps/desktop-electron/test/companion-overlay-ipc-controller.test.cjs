const assert = require("node:assert/strict");
const test = require("node:test");

const {
  CHANNELS,
  EVENT_CHANNEL,
  READY_CHANNEL,
  CompanionOverlayIpcController,
} = require("../src/companion/overlay-ipc-controller.cjs");

function fixture(overrides = {}) {
  const handlers = new Map();
  const listeners = new Map();
  const removedHandlers = [];
  const removedListeners = [];
  const calls = { submit: [], perform: [], acknowledge: [], close: 0, panels: [], sent: [] };
  const current = Object.freeze({ event_id: "event:1", kind: "chat", visual_state: "happy", text: "你好", actions: [], requires_ack: false });
  const sender = { id: 7, send: (channel, payload) => calls.sent.push({ channel, payload }) };
  const overlayController = {
    current,
    submit: (payload) => { calls.submit.push(payload); return { status: "submitted" }; },
    perform: (payload) => { calls.perform.push(payload); return { status: "performed" }; },
    acknowledge: (payload) => { calls.acknowledge.push(payload); return { status: "acknowledged" }; },
    close: () => { calls.close += 1; return { status: "closed" }; },
  };
  const options = {
    ipcMain: {
      handle: (channel, handler) => handlers.set(channel, handler),
      removeHandler: (channel) => { removedHandlers.push(channel); handlers.delete(channel); },
      on: (channel, listener) => listeners.set(channel, listener),
      removeListener: (channel, listener) => {
        removedListeners.push({ channel, listener });
        if (listeners.get(channel) === listener) listeners.delete(channel);
      },
    },
    overlayWindowProvider: () => ({ isDestroyed: () => false, webContents: { id: 7 } }),
    overlayController,
    validatePanelPayload: (payload) => {
      if (!payload || Object.keys(payload).join() !== "panel" || payload.panel !== "chat") throw new Error("companion_panel_payload_rejected");
      return payload;
    },
    openCenter: (panel) => { calls.panels.push(panel); return { status: "opened", panel }; },
    ...overrides,
  };
  return {
    controller: new CompanionOverlayIpcController(options), handlers, listeners,
    removedHandlers, removedListeners, calls, current, sender, overlayController,
  };
}

test("installs and disposes the fixed overlay IPC surface", () => {
  const value = fixture();
  assert.equal(value.controller.install(), true);
  assert.deepEqual([...value.handlers.keys()], CHANNELS);
  assert.deepEqual([...value.listeners.keys()], [READY_CHANNEL]);
  assert.equal(value.controller.install(), false);
  assert.equal(value.controller.dispose(), true);
  assert.deepEqual(value.removedHandlers, CHANNELS);
  assert.equal(value.removedListeners.length, 1);
  assert.equal(value.removedListeners[0].channel, READY_CHANNEL);
  assert.equal(value.controller.dispose(), false);
});

test("every invoke rejects a non-overlay renderer before delegation", async () => {
  const value = fixture();
  value.controller.install();
  const rejectedEvent = { sender: { id: 8 } };
  for (const channel of CHANNELS) {
    await assert.rejects(Promise.resolve().then(() => value.handlers.get(channel)(rejectedEvent, { panel: "chat" })), /ipc_overlay_sender_rejected/);
  }
  assert.deepEqual(value.calls, { submit: [], perform: [], acknowledge: [], close: 0, panels: [], sent: [] });
});

test("ready silently ignores unknown senders and replays only the current bounded event", () => {
  const value = fixture();
  value.controller.install();
  const ready = value.listeners.get(READY_CHANNEL);
  assert.equal(ready({ sender: { id: 8, send: () => assert.fail("unexpected send") } }), false);
  assert.equal(ready({ sender: value.sender }), true);
  assert.deepEqual(value.calls.sent, [{ channel: EVENT_CHANNEL, payload: value.current }]);
  value.overlayController.current = null;
  assert.equal(ready({ sender: value.sender }), true);
  assert.equal(value.calls.sent.length, 1);
});

test("submit action acknowledge and close delegate without duplicating domain rules", async () => {
  const value = fixture();
  value.controller.install();
  const event = { sender: value.sender };
  const submit = { request_id: "request:1", text: "你好" };
  const action = { event_id: "event:1", action_id: "open" };
  const ack = { event_id: "event:1" };
  assert.deepEqual(await value.handlers.get(CHANNELS[0])(event, submit), { status: "submitted" });
  assert.deepEqual(await value.handlers.get(CHANNELS[1])(event, action), { status: "performed" });
  assert.deepEqual(await value.handlers.get(CHANNELS[2])(event, ack), { status: "acknowledged" });
  assert.deepEqual(value.handlers.get(CHANNELS[3])(event), { status: "closed" });
  assert.deepEqual(value.calls.submit, [submit]);
  assert.deepEqual(value.calls.perform, [action]);
  assert.deepEqual(value.calls.acknowledge, [ack]);
  assert.equal(value.calls.close, 1);
});

test("open center validates the fixed panel payload before navigation", () => {
  const value = fixture();
  value.controller.install();
  const event = { sender: value.sender };
  assert.deepEqual(value.handlers.get(CHANNELS[4])(event, { panel: "chat" }), { status: "opened", panel: "chat" });
  assert.throws(() => value.handlers.get(CHANNELS[4])(event, { panel: "chat", url: "https://example.com" }), /payload_rejected/);
  assert.deepEqual(value.calls.panels, ["chat"]);
});

test("failed installation rolls back every registered handler", () => {
  const removed = [];
  let count = 0;
  const value = fixture({
    ipcMain: {
      handle: () => { count += 1; if (count === 3) throw new Error("registration_failed"); },
      removeHandler: (channel) => removed.push(channel),
      on: () => assert.fail("ready listener must not be installed"),
      removeListener: () => assert.fail("ready listener must not be removed"),
    },
  });
  assert.throws(() => value.controller.install(), /registration_failed/);
  assert.deepEqual(removed, CHANNELS.slice(0, 2));
  assert.equal(value.controller.installed, false);
});
