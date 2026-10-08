const assert = require("node:assert/strict");
const test = require("node:test");

const { CompanionAppearanceArbiter, PRIORITY } = require("../src/companion/appearance-arbiter.cjs");

const projections = {
  idle: { state: "ready", mood: "calm", animation_key: "idle" },
  schedule: { state: "sleeping", mood: "idle", animation_key: "sleep" },
  interactive: { state: "speaking", mood: "curious", animation_key: "talk" },
  motion: { state: "working", mood: "focused", animation_key: "falling" },
  system: { state: "warning", mood: "idle", animation_key: "warn" },
  critical: { state: "warning", mood: "idle", animation_key: "warn" },
};

test("arbitrates critical, motion, interactive, schedule and idle in fixed priority", () => {
  const emitted = [];
  const arbiter = new CompanionAppearanceArbiter({ emit: (value) => emitted.push(value) });
  arbiter.set("idle", projections.idle);
  arbiter.set("schedule", projections.schedule);
  arbiter.set("interactive", projections.interactive);
  arbiter.set("motion", projections.motion);
  arbiter.set("system", projections.system);
  arbiter.set("critical", projections.critical);
  arbiter.set("idle", { state: "attention", mood: "curious", animation_key: "listen" });
  assert.deepEqual(PRIORITY, { idle: 100, schedule: 200, interactive: 300, motion: 400, system: 450, critical: 500 });
  assert.equal(emitted.at(-1).state, "warning");
  const count = emitted.length;
  arbiter.clear("critical");
  assert.equal(emitted.length, count + 1);
  assert.equal(emitted.at(-1).animation_key, "warn");
  arbiter.clear("system");
  assert.equal(emitted.at(-1).animation_key, "falling");
  arbiter.clear("motion");
  assert.equal(emitted.at(-1).animation_key, "talk");
  arbiter.clear("interactive");
  assert.equal(emitted.at(-1).animation_key, "sleep");
  arbiter.clear("schedule");
  assert.equal(emitted.at(-1).animation_key, "listen");
  assert.deepEqual(Object.keys(emitted.at(-1)).sort(), ["animation_key", "mood", "revision", "state"]);
  const delivered = emitted.at(-1);
  arbiter.deliverCurrent();
  assert.equal(emitted.at(-1), delivered);
});

test("expires temporary lanes and restores the latest lower-priority projection", () => {
  let now = 1_000;
  let timeout;
  const emitted = [];
  const arbiter = new CompanionAppearanceArbiter({
    emit: (value) => emitted.push(value), now: () => now,
    setTimeoutImpl: (callback, delay) => { timeout = { callback, delay }; return 7; },
    clearTimeoutImpl: () => {},
  });
  arbiter.set("idle", projections.idle);
  arbiter.set("interactive", projections.interactive, { ttlMs: 1_500 });
  arbiter.set("idle", { state: "working", mood: "analyzing", animation_key: "listen" });
  assert.equal(timeout.delay, 1_500);
  now = 2_500;
  timeout.callback();
  assert.equal(emitted.at(-1).state, "working");
  assert.equal(emitted.at(-1).revision > emitted.at(-2).revision, true);
});

test("rejects unknown channels, animations, sensitive fields and unsafe TTLs", () => {
  const arbiter = new CompanionAppearanceArbiter({ emit: () => {} });
  assert.throws(() => arbiter.set("root", projections.idle), /channel/);
  assert.throws(() => arbiter.set("idle", { ...projections.idle, text: "secret" }), /unsupported/);
  assert.throws(() => arbiter.set("idle", { ...projections.idle, animation_key: "../../run" }), /animation/);
  assert.throws(() => arbiter.set("idle", projections.idle, { ttlMs: 1 }), /ttl/);
});
