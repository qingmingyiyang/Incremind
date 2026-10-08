const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const { CompanionEasterEggController, loadCatalog, periodKey } = require("../src/companion/easter-egg-controller.cjs");
const { createTemporaryRootTracker } = require("./support/temporary-root.cjs");

const temporaryRoot = createTemporaryRootTracker(test);

function fixture() {
  const root = temporaryRoot("chriptmas-easter-");
  const catalogPath = path.join(root, "behaviors.json");
  const statePath = path.join(root, "state.json");
  fs.writeFileSync(catalogPath, JSON.stringify({ version: 1, events: [
    { id: "pet_five", enabled: true, counter: "gesture.pet", threshold: 5, reset: "daily", cooldown_seconds: 60, actions: [{ type: "show_text", text: "摸了五次。" }, { type: "visual_state", state: "happy" }] },
    { id: "game_three", enabled: true, counter: "minigame.play", threshold: 3, reset: "weekly", cooldown_seconds: 0, actions: [{ type: "show_text", text: "三局完成。" }] },
  ] }));
  return { root, catalogPath, statePath };
}

test("persists counters and returns only a bounded projection at threshold", () => {
  const value = fixture();
  let now = new Date(2026, 6, 20, 10, 0, 0);
  const controller = new CompanionEasterEggController({ ...value, now: () => now });
  assert.deepEqual(controller.status(), { enabled: true, state: "ready", available_events: 2 });
  for (let index = 0; index < 4; index += 1) assert.deepEqual(controller.record("gesture.pet").events, []);
  assert.deepEqual(controller.record("gesture.pet").events, [{ id: "pet_five", text: "摸了五次。", visual_state: "happy" }]);
  const stored = JSON.parse(fs.readFileSync(value.statePath, "utf8"));
  assert.equal(stored.counters["gesture.pet"].count, 5);
  for (const forbidden of ["affinity", "wallet", "coins", "provider", "prompt"]) assert.equal(JSON.stringify(stored).includes(forbidden), false);
  const restarted = new CompanionEasterEggController({ ...value, now: () => now });
  assert.equal(restarted.record("gesture.pet").events.length, 0);
  now = new Date(2026, 6, 20, 10, 2, 0);
  for (let index = 0; index < 3; index += 1) assert.equal(restarted.record("gesture.pet").events.length, 0);
  assert.equal(restarted.record("gesture.pet").events[0].id, "pet_five");
  now = new Date(2026, 6, 21, 10, 0, 0);
  assert.equal(restarted.record("gesture.pet").events.length, 0);
});

test("supports a durable feature flag and weekly counter", () => {
  const value = fixture();
  const controller = new CompanionEasterEggController({ ...value, now: () => new Date(2026, 6, 20, 10, 0, 0) });
  assert.equal(controller.setEnabled(false).enabled, false);
  assert.equal(controller.record("minigame.play").status, "disabled");
  assert.equal(controller.setEnabled(true).enabled, true);
  controller.record("minigame.play");
  controller.record("minigame.play");
  assert.deepEqual(controller.record("minigame.play").events, [{ id: "game_three", text: "三局完成。", visual_state: "surprised" }]);
});

test("fails closed on corrupt state and refuses to overwrite it", () => {
  const value = fixture();
  fs.writeFileSync(value.statePath, "not-json");
  const controller = new CompanionEasterEggController(value);
  assert.deepEqual(controller.status(), { enabled: false, state: "invalid", available_events: 2 });
  assert.equal(controller.record("gesture.pet").status, "disabled");
  assert.throws(() => controller.setEnabled(true), /state_invalid/);
  assert.equal(fs.readFileSync(value.statePath, "utf8"), "not-json");
});

test("rejects executable catalog fields, duplicate counters and invalid clocks", () => {
  const value = fixture();
  const payload = JSON.parse(fs.readFileSync(value.catalogPath, "utf8"));
  payload.events[0].script = "run()";
  fs.writeFileSync(value.catalogPath, JSON.stringify(payload));
  assert.throws(() => loadCatalog(value.catalogPath), /event_invalid/);
  delete payload.events[0].script;
  payload.events[1].counter = payload.events[0].counter;
  fs.writeFileSync(value.catalogPath, JSON.stringify(payload));
  assert.throws(() => loadCatalog(value.catalogPath), /event_invalid/);
  const valid = fixture();
  const controller = new CompanionEasterEggController({ ...valid, now: () => new Date("bad") });
  assert.throws(() => controller.record("gesture.pet"), /clock_invalid/);
  assert.throws(() => controller.record("unknown.counter"), /counter_unknown/);
});

test("daily and weekly periods use local calendar boundaries", () => {
  assert.equal(periodKey("daily", new Date(2026, 6, 20, 23, 59)), "2026-07-20");
  assert.equal(periodKey("weekly", new Date(2026, 6, 20, 0, 1)), "2026-07-20");
  assert.equal(periodKey("weekly", new Date(2026, 6, 26, 23, 59)), "2026-07-20");
  assert.equal(periodKey("never", new Date()), "all");
});
