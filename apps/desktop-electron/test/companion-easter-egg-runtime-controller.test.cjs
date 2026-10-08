const assert = require("node:assert/strict");
const test = require("node:test");

const { CompanionEasterEggRuntimeController } = require("../src/companion/easter-egg-runtime-controller.cjs");

function harness(controller = null) {
  const presented = [];
  const runtime = new CompanionEasterEggRuntimeController({
    controllerProvider: () => controller,
    overlayController: { present: (...args) => presented.push(args) },
    now: () => 46655,
  });
  return { presented, runtime };
}

test("unavailable domain projects fixed status and inert record result", () => {
  const value = harness();
  assert.deepEqual(value.runtime.status(), { enabled: false, state: "unavailable", available_events: 0 });
  assert.deepEqual(value.runtime.record("gesture.pet"), { status: "unavailable", events: [] });
  assert.deepEqual(value.presented, []);
  assert.throws(() => value.runtime.setEnabled(true), /companion_easter_egg_unavailable/);
});

test("runtime delegates status and settings without copying persistence rules", () => {
  const calls = [];
  const value = harness({
    status() { calls.push(["status"]); return { enabled: true, state: "ready", available_events: 2 }; },
    setEnabled(enabled) { calls.push(["enabled", enabled]); return { enabled, state: "ready", available_events: 2 }; },
  });
  assert.equal(value.runtime.status().enabled, true);
  assert.equal(value.runtime.setEnabled(false).enabled, false);
  assert.deepEqual(calls, [["status"], ["enabled", false]]);
});

test("record failure stays unavailable while valid events become bounded non-focusing overlays", () => {
  const failed = harness({ record() { throw new Error("corrupt"); } });
  assert.equal(failed.runtime.record("gesture.pet").status, "unavailable");
  assert.deepEqual(failed.presented, []);

  const result = Object.freeze({ status: "recorded", events: Object.freeze([
    Object.freeze({ id: "pet_five", text: "摸了五次。", visual_state: "happy" }),
  ]) });
  const value = harness({ record(counter) { assert.equal(counter, "gesture.pet"); return result; } });
  assert.equal(value.runtime.record("gesture.pet"), result);
  assert.deepEqual(value.presented, [[{
    event_id: "easter-egg:pet_five:zzz",
    kind: "easter_egg",
    visual_state: "happy",
    text: "摸了五次。",
    actions: [],
    requires_ack: false,
  }, { focus: false }]]);
});
