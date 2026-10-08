const assert = require("node:assert/strict");
const test = require("node:test");

const { CHANNELS, CompanionEasterEggIpcController } = require("../src/companion/easter-egg-ipc-controller.cjs");

function harness({ failAt = null } = {}) {
  const calls = [];
  const handlers = new Map();
  const removed = [];
  const runtime = {
    status() { calls.push(["status"]); return { enabled: true }; },
    setEnabled(enabled) { calls.push(["enabled", enabled]); return { enabled }; },
    record(counter) { calls.push(["record", counter]); return { status: "recorded", events: [] }; },
  };
  const controller = new CompanionEasterEggIpcController({
    ipcMain: {
      handle(channel, handler) {
        if (channel === failAt) throw new Error("registration_failed");
        handlers.set(channel, handler);
      },
      removeHandler(channel) { removed.push(channel); handlers.delete(channel); },
    },
    requireMainRenderer(event) {
      calls.push(["guard", event]);
      if (!event?.trusted) throw new Error("ipc_main_sender_rejected");
    },
    runtime,
  });
  return { calls, controller, handlers, removed };
}

const event = Object.freeze({ trusted: true });

test("controller owns the exact three-channel surface and disposes once", () => {
  const value = harness();
  assert.equal(value.controller.install(), true);
  assert.equal(value.controller.install(), false);
  assert.deepEqual([...value.handlers.keys()], [...CHANNELS]);
  assert.equal(value.controller.dispose(), true);
  assert.equal(value.controller.dispose(), false);
  assert.deepEqual(value.removed, [...CHANNELS]);
});

test("all operations reject an unknown renderer before runtime access", async () => {
  const value = harness();
  value.controller.install();
  for (const [channel, handler] of value.handlers) {
    assert.throws(() => handler({ trusted: false }, {}), /ipc_main_sender_rejected/, channel);
  }
  assert.deepEqual(value.calls.filter(([name]) => name !== "guard"), []);
});

test("status settings and local action keep exact DTO contracts", () => {
  const value = harness();
  value.controller.install();
  assert.deepEqual(value.handlers.get(CHANNELS[0])(event), { enabled: true });
  assert.deepEqual(value.handlers.get(CHANNELS[1])(event, { enabled: false }), { enabled: false });
  assert.deepEqual(value.handlers.get(CHANNELS[2])(event, { action: "minigame_play" }), { status: "recorded", events: [] });
  assert.deepEqual(value.calls.filter(([name]) => name !== "guard"), [
    ["status"], ["enabled", false], ["record", "minigame.play"],
  ]);
  for (const payload of [null, {}, { enabled: true, extra: 1 }, { enabled: "yes" }]) {
    assert.throws(() => value.handlers.get(CHANNELS[1])(event, payload), /setting_rejected/);
  }
  for (const payload of [null, {}, { action: "gesture.pet" }, { action: "minigame_play", extra: 1 }]) {
    assert.throws(() => value.handlers.get(CHANNELS[2])(event, payload), /local_action_rejected/);
  }
});

test("failed installation removes only handlers registered by this controller", () => {
  const value = harness({ failAt: CHANNELS[1] });
  assert.throws(() => value.controller.install(), /registration_failed/);
  assert.deepEqual(value.removed, [CHANNELS[0]]);
  assert.equal(value.handlers.size, 0);
  assert.equal(value.controller.installed, false);
});
