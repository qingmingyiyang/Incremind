const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { CompanionWeatherRuntimeController } = require("../src/companion/weather-runtime-controller.cjs");

function fixture(status) {
  const projections = [], extremes = [], errors = [], intervals = [], cleared = [];
  const controller = new CompanionWeatherRuntimeController({
    readStatus: async () => status.value,
    onProjection: (value) => projections.push(value),
    onExtreme: (value) => extremes.push(value),
    quiet: () => status.quiet === true,
    onError: (error) => errors.push(error),
    setIntervalFn: (handler, delay) => { intervals.push({ handler, delay }); return intervals.length; },
    clearIntervalFn: (id) => cleared.push(id),
  });
  return { controller, projections, extremes, errors, intervals, cleared };
}

test("disabled weather projects unknown and does not poll", async () => {
  const status = { value: { config: { enabled: false }, weather: { condition: "rain", is_day: true, stale: false, revision: 99 } } };
  const current = fixture(status);
  await current.controller.refresh();
  assert.deepEqual(current.projections, [{ condition: "unknown", is_day: null, stale: true, revision: 99 }]);
  assert.equal(current.intervals.length, 0);
});

test("enabled weather polls and exposes only bounded coarse projection", async () => {
  const status = { value: { config: { enabled: true, location_name: "private", latitude: 31.2 }, weather: { condition: "rain", is_day: true, stale: false, revision: 4, temperature_c: 21, fetched_at: "2026-07-23T00:00:00Z" } } };
  const current = fixture(status);
  await current.controller.refresh();
  assert.deepEqual(current.controller.current(), { condition: "rain", is_day: true, stale: false, revision: 4 });
  assert.equal(JSON.stringify(current.projections).includes("private"), false);
  assert.equal(JSON.stringify(current.projections).includes("31.2"), false);
  assert.equal(current.intervals.length, 1);
  await current.intervals[0].handler();
  assert.equal(current.intervals.length, 1);
});

test("invalid sidecar values fail closed", () => {
  const status = { value: null };
  const current = fixture(status);
  current.controller.apply({ config: { enabled: true }, weather: { condition: "script", is_day: "yes", stale: false, revision: -1 } });
  assert.deepEqual(current.controller.current(), { condition: "unknown", is_day: null, stale: true, revision: 0 });
});

test("ships the attribution privacy and disable notice inside the packaged src boundary", () => {
  const notice = fs.readFileSync(path.join(__dirname, "..", "src", "companion", "OPEN_METEO_NOTICE.md"), "utf8");
  assert.match(notice, /CC BY 4\.0/);
  assert.match(notice, /90 days/);
  assert.match(notice, /disabled by default/);
  const packageJson = require("../package.json");
  assert.equal(packageJson.build.files.includes("src/**/*"), true);
});

test("extreme notice is deduplicated and suppressed while quiet", () => {
  const status = { value: null, quiet: false };
  const current = fixture(status);
  const extreme = { config: { enabled: true }, weather: { condition: "extreme", is_day: false, stale: false, revision: 3, fetched_at: "2026-07-23T00:00:00Z" } };
  current.controller.apply(extreme);
  current.controller.apply(extreme);
  assert.deepEqual(current.extremes, [{ condition: "extreme", revision: 3 }]);
  status.quiet = true;
  current.controller.apply({ ...extreme, weather: { ...extreme.weather, revision: 4, fetched_at: "2026-07-23T01:00:00Z" } });
  assert.equal(current.extremes.length, 1);
});

test("discards an older enabled projection but still lets disable clear the pet", () => {
  const status = { value: null };
  const current = fixture(status);
  current.controller.apply({ config: { enabled: true }, weather: { condition: "rain", is_day: true, stale: false, revision: 8 } });
  current.controller.apply({ config: { enabled: true }, weather: { condition: "clear", is_day: true, stale: false, revision: 7 } });
  assert.deepEqual(current.controller.current(), { condition: "rain", is_day: true, stale: false, revision: 8 });
  assert.equal(current.projections.length, 1);
  current.controller.apply({ config: { enabled: false }, weather: { revision: 0 } });
  assert.deepEqual(current.controller.current(), { condition: "unknown", is_day: null, stale: true, revision: 0 });
});

test("disable stops interval, errors stay local and dispose resets projection", async () => {
  const status = { value: { config: { enabled: true }, weather: { condition: "clear", is_day: true, stale: false, revision: 1 } } };
  const current = fixture(status);
  await current.controller.refresh();
  status.value = { config: { enabled: false }, weather: {} };
  await current.controller.refresh();
  assert.deepEqual(current.cleared, [1]);
  status.value = null;
  current.controller.readStatus = async () => { throw new Error("coordinate-canary"); };
  assert.equal(await current.controller.refresh(), null);
  assert.equal(current.errors.length, 1);
  current.controller.dispose();
  assert.deepEqual(current.controller.current(), { condition: "unknown", is_day: null, stale: true, revision: 0 });
});
