const assert = require("node:assert/strict");
const test = require("node:test");

const { CHANNELS, CompanionRoutineIpcController } = require("../src/companion/routine-ipc-controller.cjs");

function fixture(overrides = {}) {
  const handlers = new Map();
  const removed = [];
  const calls = [];
  let wakeResult = Object.freeze({ status: "awakened", sleeping: false });
  const routineController = {
    status: () => { calls.push(["status"]); return { sleeping: true }; },
    wakeForThirtyMinutes: () => { calls.push(["wake"]); return wakeResult; },
  };
  const options = {
    ipcMain: {
      handle: (channel, handler) => handlers.set(channel, handler),
      removeHandler: (channel) => { removed.push(channel); handlers.delete(channel); },
    },
    requireMainRenderer: () => calls.push(["guard"]),
    routineController,
    refreshRoutine: async () => { calls.push(["refresh"]); return { settings_revision: 2 }; },
    presentOverlay: (event, settings) => calls.push(["overlay", event, settings]),
    now: () => 36,
    ...overrides,
  };
  return {
    controller: new CompanionRoutineIpcController(options), handlers, removed, calls, routineController,
    setWakeResult: (value) => { wakeResult = value; },
  };
}

test("installs and disposes the fixed routine IPC allowlist", () => {
  const value = fixture();
  assert.equal(value.controller.install(), true);
  assert.deepEqual([...value.handlers.keys()], CHANNELS);
  assert.equal(value.controller.install(), false);
  assert.equal(value.controller.dispose(), true);
  assert.deepEqual(value.removed, CHANNELS);
  assert.equal(value.controller.dispose(), false);
});

test("every routine operation validates the main renderer before work", async () => {
  const calls = [];
  const value = fixture({
    requireMainRenderer: () => { calls.push("guard"); throw new Error("ipc_main_sender_rejected"); },
    routineController: {
      status: () => { calls.push("status"); },
      wakeForThirtyMinutes: () => { calls.push("wake"); },
    },
    refreshRoutine: () => { calls.push("refresh"); },
    presentOverlay: () => calls.push("overlay"),
  });
  value.controller.install();
  for (const channel of CHANNELS) {
    await assert.rejects(Promise.resolve().then(() => value.handlers.get(channel)({})), /ipc_main_sender_rejected/);
  }
  assert.deepEqual(calls, ["guard", "guard", "guard"]);
});

test("status and refresh delegate without duplicating routine rules", async () => {
  const value = fixture();
  value.controller.install();
  assert.deepEqual(value.handlers.get(CHANNELS[0])({}), { sleeping: true });
  assert.deepEqual(await value.handlers.get(CHANNELS[1])({}), { settings_revision: 2 });
  assert.deepEqual(value.calls, [["guard"], ["status"], ["guard"], ["refresh"]]);
});

test("an awakened routine presents the fixed non-focusing overlay", () => {
  const value = fixture();
  value.controller.install();
  assert.deepEqual(value.handlers.get(CHANNELS[2])({}), { status: "awakened", sleeping: false });
  assert.deepEqual(value.calls, [
    ["guard"],
    ["wake"],
    ["overlay", {
      event_id: "routine:wake:10",
      kind: "routine_wake",
      visual_state: "attention",
      text: "我先醒来陪你半小时。",
      actions: [],
      requires_ack: false,
    }, { focus: false }],
  ]);
});

test("already-awake and unavailable wake results remain presentation-free", () => {
  for (const status of ["already_awake", "unavailable"]) {
    const value = fixture();
    value.setWakeResult(Object.freeze({ status, sleeping: status === "unavailable" }));
    value.controller.install();
    assert.equal(value.handlers.get(CHANNELS[2])({}).status, status);
    assert.deepEqual(value.calls, [["guard"], ["wake"]]);
  }
});

test("failed installation rolls back registered routine channels", () => {
  const removed = [];
  let count = 0;
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
