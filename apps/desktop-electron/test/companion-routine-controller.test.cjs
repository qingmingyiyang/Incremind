const assert = require("node:assert/strict");
const test = require("node:test");

const { CompanionRoutineController, WAKE_DURATION_MS, isRoutineSleep, sanitiseRoutineSettings } = require("../src/companion/routine-controller.cjs");

const SETTINGS = { enabled: true, sleep_start: "23:00", wake_time: "07:00" };

test("recognises cross-midnight, daytime and exact minute boundaries", () => {
  assert.equal(isRoutineSleep(new Date(2026, 6, 20, 23, 0), SETTINGS), true);
  assert.equal(isRoutineSleep(new Date(2026, 6, 21, 6, 59), SETTINGS), true);
  assert.equal(isRoutineSleep(new Date(2026, 6, 21, 7, 0), SETTINGS), false);
  const daytime = { enabled: true, sleep_start: "10:00", wake_time: "14:00" };
  assert.equal(isRoutineSleep(new Date(2026, 6, 21, 12, 0), daytime), true);
  assert.equal(isRoutineSleep(new Date(2026, 6, 21, 22, 0), daytime), false);
});

test("rejects unsafe, incomplete and equal-time settings", () => {
  assert.equal(sanitiseRoutineSettings({ ...SETTINGS, path: "C:/secret" }), null);
  assert.equal(sanitiseRoutineSettings({ enabled: true, sleep_start: "23:00" }), null);
  assert.equal(sanitiseRoutineSettings({ enabled: true, sleep_start: "07:00", wake_time: "07:00" }), null);
  const controller = new CompanionRoutineController({ publish: () => {}, clear: () => {} });
  assert.equal(controller.applySnapshot({ revision: 0, settings: { ...SETTINGS, sleep_start: "22:00" } }), false);
});

test("publishes sleep on the schedule lane and restores it after a fixed manual wake", () => {
  let wall = new Date(2026, 6, 20, 23, 30);
  let mono = 1000;
  const values = [];
  const controller = new CompanionRoutineController({
    now: () => wall, monotonicNow: () => mono,
    publish: (value) => values.push(value), clear: () => values.push("clear"),
  });
  assert.equal(controller.applySnapshot({ revision: 1, settings: SETTINGS }), true);
  assert.equal(controller.status().sleeping, true);
  assert.equal(controller.wakeForThirtyMinutes().status, "awakened");
  assert.equal(controller.status().sleeping, false);
  mono += WAKE_DURATION_MS - 1;
  controller.evaluate();
  assert.equal(controller.status().sleeping, false);
  mono += 1;
  controller.evaluate();
  assert.equal(controller.status().sleeping, true);
  assert.deepEqual(values.map((value) => value?.state || value), ["sleeping", "clear", "sleeping"]);
});

test("monotonic rollback cancels temporary wake and morning attempts once per local day", async () => {
  let wall = new Date(2026, 6, 21, 6, 30);
  let mono = 5000;
  const mornings = [];
  const controller = new CompanionRoutineController({
    now: () => wall, monotonicNow: () => mono, publish: () => {}, clear: () => {},
    onMorning: (day) => mornings.push(day),
  });
  controller.applySnapshot({ revision: 1, settings: SETTINGS });
  controller.wakeForThirtyMinutes();
  mono = 4000;
  controller.evaluate();
  assert.equal(controller.status().sleeping, true);
  wall = new Date(2026, 6, 21, 7, 0);
  mono = 6000;
  controller.evaluate();
  controller.evaluate();
  wall = new Date(2026, 6, 22, 7, 0);
  controller.evaluate();
  await Promise.resolve();
  assert.deepEqual(mornings, ["2026-07-21", "2026-07-22"]);
});
