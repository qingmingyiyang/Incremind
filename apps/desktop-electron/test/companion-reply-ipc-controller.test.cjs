const assert = require("node:assert/strict");
const test = require("node:test");

const { CHANNEL, CompanionReplyIpcController } = require("../src/companion/reply-ipc-controller.cjs");

function harness() {
  const calls = [];
  const handlers = new Map();
  const removed = [];
  const controller = new CompanionReplyIpcController({
    ipcMain: {
      handle: (channel, handler) => handlers.set(channel, handler),
      removeHandler(channel) { removed.push(channel); handlers.delete(channel); },
    },
    requireMainRenderer(event) {
      calls.push(["guard", event]);
      if (!event?.trusted) throw new Error("ipc_main_sender_rejected");
    },
    presentation: {
      present(text) { calls.push(["present", text]); return { status: "presented" }; },
    },
  });
  controller.install();
  return { calls, controller, handlers, removed };
}

const event = Object.freeze({ trusted: true });

test("reply IPC validates sender and exact text DTO before presentation", () => {
  const value = harness();
  assert.throws(() => value.handlers.get(CHANNEL)({ trusted: false }, { text: "hi" }), /ipc_main_sender_rejected/);
  assert.deepEqual(value.calls, [["guard", { trusted: false }]]);
  for (const payload of [null, {}, { text: 1 }, { text: "hi", extra: true }]) {
    assert.throws(() => value.handlers.get(CHANNEL)(event, payload), /payload_rejected/);
  }
  assert.deepEqual(value.handlers.get(CHANNEL)(event, { text: "收到。" }), { status: "presented" });
  assert.deepEqual(value.calls.at(-1), ["present", "收到。"]);
});

test("reply IPC owns and disposes its one channel idempotently", () => {
  const value = harness();
  assert.equal(value.controller.install(), false);
  assert.equal(value.controller.dispose(), true);
  assert.equal(value.controller.dispose(), false);
  assert.deepEqual(value.removed, [CHANNEL]);
});
