const assert = require("node:assert/strict");
const test = require("node:test");

const { CHANNELS, CompanionMultiCharacterIpcController } = require("../src/companion/multicharacter-ipc-controller.cjs");

function fixture(overrides = {}) {
  const handlers = new Map();
  const removed = [];
  const calls = [];
  const runtimeController = {
    projection: () => { calls.push(["projection"]); return { state: "ready" }; },
    configure: async (payload) => { calls.push(["configure", payload]); return { state: "ready", revision: 2 }; },
    sendAction: async (instanceId, action) => { calls.push(["send", instanceId, action]); return { status: "accepted" }; },
  };
  const options = {
    ipcMain: {
      handle: (channel, handler) => handlers.set(channel, handler),
      removeHandler: (channel) => { removed.push(channel); handlers.delete(channel); },
    },
    requireMainRenderer: (event) => {
      calls.push(["guard", event?.sender]);
      if (event?.sender !== "main") throw new Error("ipc_window_sender_rejected");
    },
    runtimeController,
    ...overrides,
  };
  return { controller: new CompanionMultiCharacterIpcController(options), handlers, removed, calls, runtimeController };
}

test("installs and disposes the exact multi-character invoke surface", () => {
  const value = fixture();
  assert.equal(value.controller.install(), true);
  assert.deepEqual([...value.handlers.keys()], CHANNELS);
  assert.equal(value.controller.install(), false);
  assert.equal(value.controller.dispose(), true);
  assert.deepEqual(value.removed, CHANNELS);
  assert.equal(value.controller.dispose(), false);
});

test("all operations reject a non-main renderer before runtime access", async () => {
  const value = fixture();
  value.controller.install();
  assert.throws(() => value.handlers.get(CHANNELS[0])({ sender: "pet" }), /ipc_window_sender_rejected/);
  assert.throws(() => value.handlers.get(CHANNELS[1])({ sender: "pet" }, {}), /ipc_window_sender_rejected/);
  assert.throws(() => value.handlers.get(CHANNELS[2])({ sender: "pet" }, {}), /ipc_window_sender_rejected/);
  assert.equal(value.calls.some(([name]) => ["projection", "configure", "send"].includes(name)), false);
});

test("status and exact configuration delegate without copying settings rules", async () => {
  const value = fixture();
  value.controller.install();
  assert.deepEqual(value.handlers.get(CHANNELS[0])({ sender: "main" }), { state: "ready" });
  const payload = {
    enabled: true,
    consented: true,
    character_id: "chriptmas.bear",
    allowed_character_ids: ["friend.cat"],
    expected_revision: 1,
  };
  assert.deepEqual(await value.handlers.get(CHANNELS[1])({ sender: "main" }, payload), { state: "ready", revision: 2 });
  assert.deepEqual(value.calls.find(([name]) => name === "configure"), ["configure", payload]);
  for (const invalid of [null, [], {}, { ...payload, token: "secret" }, { ...payload, allowed_character_ids: "friend.cat" }]) {
    assert.throws(() => value.handlers.get(CHANNELS[1])({ sender: "main" }, invalid), /companion_multicharacter_payload_rejected/);
  }
});

test("send accepts only the three fixed local actions and bounded peer identity", async () => {
  const value = fixture();
  value.controller.install();
  for (const action of ["wave", "greeting", "cheer"]) {
    assert.deepEqual(await value.handlers.get(CHANNELS[2])({ sender: "main" }, { instance_id: "peer", action }), { status: "accepted" });
  }
  for (const invalid of [null, [], {}, { instance_id: "peer", action: "command" }, { instance_id: 1, action: "wave" }, { instance_id: "peer", action: "wave", text: "hello" }]) {
    assert.throws(() => value.handlers.get(CHANNELS[2])({ sender: "main" }, invalid), /companion_multicharacter_payload_rejected/);
  }
});

test("failed installation rolls back only already registered handlers", () => {
  let count = 0;
  const removed = [];
  const value = fixture({
    ipcMain: {
      handle: () => { count += 1; if (count === 3) throw new Error("registration_failed"); },
      removeHandler: (channel) => removed.push(channel),
    },
  });
  assert.throws(() => value.controller.install(), /registration_failed/);
  assert.deepEqual(removed, CHANNELS.slice(0, 2));
  assert.equal(value.controller.installed, false);
});
