const assert = require("node:assert/strict");
const test = require("node:test");
const { CompanionStateController, projectionFromMood, sanitiseExecutionSignal, sanitiseProjection } = require("../src/companion-state.cjs");

test("maps sidecar mood facts to a bounded companion projection", () => {
  assert.deepEqual(projectionFromMood({ mood: "calm" }), { state: "ready", mood: "calm" });
  assert.deepEqual(projectionFromMood({ mood: "curious" }), { state: "attention", mood: "curious" });
  assert.deepEqual(projectionFromMood({ mood: "focused" }), { state: "attention", mood: "focused" });
  assert.deepEqual(projectionFromMood({ mood: "calm", running_job_count: 1, title: "secret" }), { state: "ready", mood: "calm" });
  assert.deepEqual(projectionFromMood({ mood: "calm", execution: { effect: "active", lease: "valid" } }), { state: "working", mood: "analyzing" });
  assert.deepEqual(projectionFromMood({ mood: "calm", execution: { effect: "active", lease: "unavailable" } }), { state: "attention", mood: "focused" });
  assert.deepEqual(projectionFromMood({ mood: "calm", execution: { effect: "active", lease: "none" } }), { state: "ready", mood: "calm" });
  assert.deepEqual(projectionFromMood({ mood: "curious", execution: { effect: "unavailable", lease: "unavailable" } }), { state: "offline", mood: "idle" });
  assert.deepEqual(projectionFromMood({ mood: "unknown", payload: "secret" }), { state: "ready", mood: "calm" });
});

test("execution signal is closed and discards identifiers and lease details", () => {
  assert.deepEqual(sanitiseExecutionSignal({ effect: "active", lease: "valid" }), { effect: "active", lease: "valid" });
  assert.equal(sanitiseExecutionSignal({ effect: "active", lease: "valid", job_id: "secret" }), null);
  assert.equal(sanitiseExecutionSignal({ effect: "active", lease: "expired" }), null);
  assert.equal(sanitiseExecutionSignal({ effect: "inactive", lease: "unavailable" }), null);
  assert.equal(sanitiseExecutionSignal({ effect: "unavailable", lease: "none" }), null);
  assert.deepEqual(sanitiseExecutionSignal({ effect: "unavailable", lease: "unavailable" }), { effect: "unavailable", lease: "unavailable" });
});

test("deduplicates updates, recovers from offline, and redelivers current state", async () => {
  const emitted = [];
  const reads = [
    { state: "ready", mood: "calm" },
    { state: "ready", mood: "calm" },
    new Error("offline"),
    { state: "working", mood: "analyzing" },
  ];
  const controller = new CompanionStateController({
    readProjection: async () => {
      const next = reads.shift();
      if (next instanceof Error) throw next;
      return next;
    },
    emitProjection: (value) => emitted.push(value),
  });
  await controller.poll();
  await controller.poll();
  await controller.poll();
  await controller.poll();
  controller.deliverCurrent();
  assert.deepEqual(emitted.map(({ state, revision }) => [state, revision]), [
    ["ready", 1], ["offline", 2], ["working", 3], ["working", 3],
  ]);
});

test("start is idempotent, overlapping polls are bounded, and stop clears the timer", async () => {
  let scheduled;
  let cleared = 0;
  let resolveRead;
  let reads = 0;
  const controller = new CompanionStateController({
    readProjection: () => { reads += 1; return new Promise((resolve) => { resolveRead = resolve; }); },
    emitProjection: () => {},
    setIntervalImpl: (callback) => { scheduled = callback; return 7; },
    clearIntervalImpl: (timer) => { assert.equal(timer, 7); cleared += 1; },
  });
  controller.start();
  controller.start();
  scheduled();
  assert.equal(reads, 1);
  resolveRead({ state: "ready", mood: "calm" });
  await new Promise((resolve) => setImmediate(resolve));
  controller.stop();
  assert.equal(cleared, 1);
});

test("sanitises scheduler projections and rejects sensitive or unknown fields", () => {
  assert.deepEqual(sanitiseProjection({
    state: "speaking", mood: "curious", animation_key: "talk", revision: 4,
  }), {
    state: "speaking", mood: "curious", animation_key: "talk", revision: 4,
  });
  assert.equal(sanitiseProjection({ state: "ready", mood: "calm", revision: 1, text: "secret" }), null);
  assert.equal(sanitiseProjection({ state: "ready", mood: "calm", revision: 1, path: "C:\\private" }), null);
  assert.equal(sanitiseProjection({ state: "ready", mood: "calm", revision: 0 }), null);
  assert.equal(sanitiseProjection({ state: "ready", mood: "calm", revision: 1, animation_key: "../../run" }), null);
});

test("accepts only monotonic external revisions and rejects same-revision conflicts", () => {
  const emitted = [];
  const controller = new CompanionStateController({ readProjection: async () => ({}), emitProjection: (value) => emitted.push(value) });
  assert.equal(controller.publish({ state: "ready", mood: "calm", revision: 5 }), true);
  assert.equal(controller.publish({ state: "working", mood: "analyzing", revision: 4 }), false);
  assert.equal(controller.publish({ state: "working", mood: "analyzing", revision: 5 }), false);
  assert.equal(controller.publish({ state: "speaking", mood: "curious", animation_key: "talk", revision: 6 }), true);
  assert.deepEqual(emitted.map(({ state, revision }) => [state, revision]), [["ready", 5], ["speaking", 6]]);
});
