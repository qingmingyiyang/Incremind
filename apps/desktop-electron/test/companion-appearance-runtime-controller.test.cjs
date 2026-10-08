const assert = require("node:assert/strict");
const test = require("node:test");
const { CompanionAppearanceRuntimeController, sanitizeAppearanceProjection } = require("../src/companion/appearance-runtime-controller.cjs");

test("sanitizes a bounded appearance projection and rejects paths or unknown ids", () => {
  const valid = { outfit_id: "red-scarf", background_id: "night", growth_stage: "partner", idle_variant: "smile", state_revision: 4 };
  assert.deepEqual(sanitizeAppearanceProjection(valid), { outfit_id: "red-scarf", background_id: "night", growth_stage: "partner", idle_variant: "smile", revision: 4 });
  assert.equal(sanitizeAppearanceProjection({ ...valid, outfit_id: "../../secret" }), null);
  assert.equal(sanitizeAppearanceProjection({ ...valid, path: "C:/secret" }), null);
});

test("polls, suppresses duplicate projections and delivers current state", async () => {
  const emitted = []; let callback;
  const controller = new CompanionAppearanceRuntimeController({ readStatus: async () => ({ outfit_id: "gold-star", background_id: "default", growth_stage: "partner", idle_variant: "smile", state_revision: 2 }), onProjection: (value) => emitted.push(value), setIntervalImpl: (fn) => { callback = fn; return 8; }, clearIntervalImpl: () => {} });
  controller.start(); await new Promise(setImmediate); await callback();
  assert.equal(emitted.length, 1); controller.deliverCurrent(); assert.equal(emitted.length, 2);
  controller.stop();
});
