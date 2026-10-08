const assert = require("node:assert/strict");
const test = require("node:test");

const { CHANNEL, CompanionSystemSensorIpcController } = require("../src/companion/system-sensor-ipc-controller.cjs");

function harness() {
  const calls = [];
  const handlers = new Map();
  const removed = [];
  const controller = new CompanionSystemSensorIpcController({
    ipcMain: {
      handle: (channel, handler) => handlers.set(channel, handler),
      removeHandler(channel) { removed.push(channel); handlers.delete(channel); },
    },
    requireMainRenderer(event) {
      calls.push(["guard", event]);
      if (!event?.trusted) throw new Error("ipc_main_sender_rejected");
    },
    sensorRuntime: {
      async refresh() { calls.push(["refresh"]); return { config: { enabled: true } }; },
    },
  });
  controller.install();
  return { calls, controller, handlers, removed };
}

test("sensor IPC guards the sender then delegates one refresh", async () => {
  const value = harness();
  assert.throws(() => value.handlers.get(CHANNEL)({ trusted: false }), /ipc_main_sender_rejected/);
  assert.deepEqual(value.calls.map(([name]) => name), ["guard"]);
  assert.deepEqual(await value.handlers.get(CHANNEL)({ trusted: true }), { config: { enabled: true } });
  assert.deepEqual(value.calls.map(([name]) => name), ["guard", "guard", "refresh"]);
});

test("sensor IPC owns and disposes its one channel idempotently", () => {
  const value = harness();
  assert.equal(value.controller.install(), false);
  assert.equal(value.controller.dispose(), true);
  assert.equal(value.controller.dispose(), false);
  assert.deepEqual(value.removed, [CHANNEL]);
});
