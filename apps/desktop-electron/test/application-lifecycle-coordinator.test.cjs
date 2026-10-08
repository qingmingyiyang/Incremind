const assert = require("node:assert/strict");
const test = require("node:test");

const { ApplicationLifecycleCoordinator } = require("../src/application-lifecycle-coordinator.cjs");

function fixture(steps = [], { replayBeforeQuit = false, ...options } = {}) {
  const listeners = new Map();
  const events = [];
  const replayEvents = [];
  const app = {
    isQuitting: false,
    on: (name, listener) => { listeners.set(name, listener); },
    removeListener: (name, listener) => {
      if (listeners.get(name) === listener) listeners.delete(name);
    },
    quit: () => {
      events.push("quit");
      if (replayBeforeQuit) {
        const event = { prevented: false, preventDefault() { this.prevented = true; } };
        replayEvents.push(event);
        listeners.get("before-quit")(event);
      }
    },
  };
  const logs = [];
  const coordinator = new ApplicationLifecycleCoordinator({
    app,
    shutdownSteps: steps,
    log: (message) => logs.push(message),
    ...options,
  });
  return { app, coordinator, events, listeners, logs, replayEvents };
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

test("installs and disposes one before-quit listener idempotently", () => {
  const value = fixture([{ name: "one", run: () => {} }]);
  assert.equal(value.coordinator.install(), true);
  assert.equal(value.coordinator.install(), false);
  assert.equal(value.listeners.size, 1);
  assert.equal(value.listeners.has("before-quit"), true);
  assert.equal(value.coordinator.dispose(), true);
  assert.equal(value.coordinator.dispose(), false);
  assert.equal(value.listeners.size, 0);
});

test("quit marks the application before delegating to Electron", () => {
  const value = fixture([{ name: "one", run: () => {} }]);
  assert.deepEqual(value.coordinator.quit(), { status: "quitting" });
  assert.equal(value.app.isQuitting, true);
  assert.deepEqual(value.events, ["quit"]);
});

test("before-quit blocks synchronously, awaits a deferred sidecar, and replays one allowed quit", async () => {
  const order = [];
  const sidecar = deferred();
  const value = fixture([
    { name: "first", run: () => order.push("first") },
    { name: "sidecar", run: () => { order.push("sidecar"); return sidecar.promise; } },
    { name: "third", run: () => order.push("third") },
  ], { replayBeforeQuit: true });
  value.coordinator.install();
  const initialEvent = { prevented: false, preventDefault() { this.prevented = true; } };
  assert.equal(value.listeners.get("before-quit")(initialEvent), true);
  assert.equal(value.coordinator.shutdown(), false);
  assert.equal(value.app.isQuitting, true);
  assert.equal(initialEvent.prevented, true);
  assert.deepEqual(order, ["first", "sidecar", "third"]);
  assert.deepEqual(value.events, []);

  sidecar.resolve();
  await value.coordinator.shutdownPromise;
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(value.events, ["quit"]);
  assert.equal(value.replayEvents.length, 1);
  assert.equal(value.replayEvents[0].prevented, false);
});

test("duplicate before-quit events remain blocked without restarting shutdown or replay", async () => {
  const sidecar = deferred();
  let calls = 0;
  const value = fixture([{ name: "sidecar", run: () => { calls += 1; return sidecar.promise; } }]);
  value.coordinator.install();
  const firstEvent = { prevented: false, preventDefault() { this.prevented = true; } };
  const duplicateEvent = { prevented: false, preventDefault() { this.prevented = true; } };

  assert.equal(value.listeners.get("before-quit")(firstEvent), true);
  assert.equal(value.listeners.get("before-quit")(duplicateEvent), false);
  assert.equal(firstEvent.prevented, true);
  assert.equal(duplicateEvent.prevented, true);
  assert.equal(calls, 1);

  sidecar.resolve();
  await value.coordinator.shutdownPromise;
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(value.events, ["quit"]);
  assert.equal(calls, 1);
});

test("shutdown deadline releases quit when a cleanup step never settles", async () => {
  let fireDeadline;
  const value = fixture([
    { name: "stuck", run: () => new Promise(() => {}) },
  ], {
    shutdownDeadlineMs: 1000,
    setTimer: (callback, delay) => { fireDeadline = callback; return delay; },
    clearTimer: () => {},
  });
  value.coordinator.install();
  const event = { prevented: false, preventDefault() { this.prevented = true; } };

  assert.equal(value.listeners.get("before-quit")(event), true);
  assert.equal(event.prevented, true);
  assert.deepEqual(value.events, []);
  fireDeadline();
  await value.coordinator.shutdownPromise;
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(value.events, ["quit"]);
  assert.deepEqual(value.coordinator.failures, [
    { name: "shutdown-deadline", message: "application_shutdown_timeout_1000ms" },
  ]);
});

test("before-quit attaches replay to a shutdown that was already started", async () => {
  const cleanup = deferred();
  const value = fixture([{ name: "cleanup", run: () => cleanup.promise }]);
  value.coordinator.install();
  assert.equal(value.coordinator.shutdown(), true);
  const event = { prevented: false, preventDefault() { this.prevented = true; } };

  assert.equal(value.listeners.get("before-quit")(event), false);
  assert.equal(event.prevented, true);
  cleanup.resolve();
  await value.coordinator.shutdownPromise;
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(value.events, ["quit"]);
});

test("sync throws and async rejections are recorded while all cleanup settles", async () => {
  const order = [];
  const value = fixture([
    { name: "broken", run: () => { throw new Error("bad\nsecret-safe"); } },
    { name: "async-broken", run: () => Promise.reject(new Error("rejected")) },
    { name: "later", run: () => order.push("later") },
  ]);
  assert.equal(value.coordinator.shutdown(), true);
  assert.deepEqual(order, ["later"]);
  await value.coordinator.shutdownPromise;
  assert.deepEqual(value.coordinator.failures, [
    { name: "broken", message: "bad secret-safe" },
    { name: "async-broken", message: "rejected" },
  ]);
  assert.deepEqual(value.logs, [
    "[lifecycle] shutdown step broken failed: bad secret-safe",
    "[lifecycle] shutdown step async-broken failed: rejected",
  ]);
});

test("constructor rejects empty duplicate or executable-name-invalid steps", () => {
  assert.throws(() => fixture([]), /application_lifecycle_steps_invalid/);
  assert.throws(() => fixture([{ name: "same", run: () => {} }, { name: "same", run: () => {} }]), /application_lifecycle_step_invalid/);
  assert.throws(() => fixture([{ name: "../unsafe", run: () => {} }]), /application_lifecycle_step_invalid/);
  assert.throws(() => fixture([{ name: "valid" }]), /application_lifecycle_step_invalid/);
  assert.throws(() => fixture([{ name: "valid", run: () => {} }], { shutdownDeadlineMs: 999 }), /application_lifecycle_deadline_invalid/);
});
