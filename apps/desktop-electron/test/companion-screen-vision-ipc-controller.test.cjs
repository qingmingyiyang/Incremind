const assert = require("node:assert/strict");
const test = require("node:test");

const { CHANNELS, CompanionScreenVisionIpcController } = require("../src/companion/screen-vision-ipc-controller.cjs");

function fixture(overrides = {}) {
  const handlers = new Map();
  const removed = [];
  const calls = { guarded: 0, listed: 0, captures: [], confirms: [], cancelled: 0 };
  const vision = {
    listSources: async () => { calls.listed += 1; return { session_id: "session", items: [] }; },
    captureSource: async (payload) => { calls.captures.push(payload); return { capture_id: "capture" }; },
    confirm: async (payload) => { calls.confirms.push(payload); return { text: "看到了" }; },
    cancel: () => { calls.cancelled += 1; return true; },
  };
  const options = {
    ipcMain: {
      handle: (channel, handler) => handlers.set(channel, handler),
      removeHandler: (channel) => { removed.push(channel); handlers.delete(channel); },
    },
    requireMainRenderer: () => { calls.guarded += 1; },
    visionProvider: () => vision,
    ...overrides,
  };
  return { controller: new CompanionScreenVisionIpcController(options), handlers, removed, calls, vision };
}

test("installs and disposes the fixed Screen Vision IPC allowlist", () => {
  const value = fixture();
  assert.equal(value.controller.install(), true);
  assert.deepEqual([...value.handlers.keys()], CHANNELS);
  assert.equal(value.controller.install(), false);
  assert.equal(value.controller.dispose(), true);
  assert.deepEqual(value.removed, CHANNELS);
  assert.equal(value.controller.dispose(), false);
});

test("all operations validate the main renderer before accessing Vision", async () => {
  const calls = [];
  const value = fixture({
    requireMainRenderer: () => { calls.push("guard"); throw new Error("ipc_main_sender_rejected"); },
    visionProvider: () => { calls.push("provider"); return null; },
  });
  value.controller.install();
  for (const channel of CHANNELS) {
    await assert.rejects(Promise.resolve().then(() => value.handlers.get(channel)({}, {})), /ipc_main_sender_rejected/);
  }
  assert.deepEqual(calls, ["guard", "guard", "guard", "guard"]);
});

test("sources capture and confirm preserve payload identity and async results", async () => {
  const value = fixture();
  value.controller.install();
  const event = { sender: 1 };
  const capturePayload = { session_id: "session", item_id: "item" };
  const confirmPayload = { capture_id: "capture", question: "这是什么", bytes: new Uint8Array([1, 2]) };
  assert.deepEqual(await value.handlers.get(CHANNELS[0])(event), { session_id: "session", items: [] });
  assert.deepEqual(await value.handlers.get(CHANNELS[1])(event, capturePayload), { capture_id: "capture" });
  assert.deepEqual(await value.handlers.get(CHANNELS[2])(event, confirmPayload), { text: "看到了" });
  assert.equal(value.calls.captures[0], capturePayload);
  assert.equal(value.calls.confirms[0], confirmPayload);
  assert.equal(value.calls.guarded, 3);
});

test("unavailable Vision is explicit except cancellation remains idempotent", async () => {
  const value = fixture({ visionProvider: () => null });
  value.controller.install();
  for (const channel of CHANNELS.slice(0, 3)) {
    await assert.rejects(Promise.resolve().then(() => value.handlers.get(channel)({}, {})), /screen_vision_unavailable/);
  }
  assert.deepEqual(value.handlers.get(CHANNELS[3])({}), { cancelled: false });
  assert.equal(value.calls.guarded, 4);
});

test("cancel projects only the bounded cancellation result", () => {
  const value = fixture();
  value.controller.install();
  assert.deepEqual(value.handlers.get(CHANNELS[3])({}), { cancelled: true });
  assert.equal(value.calls.cancelled, 1);
});

test("failed installation rolls back registered Vision channels", () => {
  const removed = [];
  let count = 0;
  const value = fixture({
    ipcMain: {
      handle: () => { count += 1; if (count === 4) throw new Error("registration_failed"); },
      removeHandler: (channel) => removed.push(channel),
    },
  });
  assert.throws(() => value.controller.install(), /registration_failed/);
  assert.deepEqual(removed, CHANNELS.slice(0, 3));
  assert.equal(value.controller.installed, false);
});
