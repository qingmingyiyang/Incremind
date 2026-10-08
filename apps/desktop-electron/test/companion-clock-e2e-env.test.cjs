"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const { MODE, MODE_KEY, SWITCH, UTC_KEY, resolveCompanionE2EClockEnv, resolveCompanionE2EClockNow, stripCompanionE2EClockEnv } = require("../src/companion/clock-e2e-env.cjs");
const { validateCompanionEnv } = require("../src/sidecar-supervisor.cjs");

const valid = { [MODE_KEY]: MODE, [UTC_KEY]: "2026-07-23T23:59:58Z" };

test("companion fixed clock requires the dedicated argv switch and exact owned environment", () => {
  assert.deepEqual(resolveCompanionE2EClockEnv({ argv: [], env: valid }), {});
  assert.deepEqual(resolveCompanionE2EClockEnv({ argv: [SWITCH], env: valid }), valid);
  assert.deepEqual(resolveCompanionE2EClockEnv({ argv: [SWITCH], env: { ...valid, CHRIPTMAS_COMPANION_E2E_CLOCK_SET: "1" } }), {});
  assert.deepEqual(resolveCompanionE2EClockEnv({ argv: [SWITCH], env: { ...valid, [UTC_KEY]: "2026-07-23T23:59:58+08:00" } }), {});
});

test("main schedule clock uses the same dual-gated fixed instant and returns fresh Date values", () => {
  assert.equal(resolveCompanionE2EClockNow({ argv: [], env: valid }), null);
  const now = resolveCompanionE2EClockNow({ argv: [SWITCH], env: valid });
  assert.equal(now().toISOString().replace(".000Z", "Z"), valid[UTC_KEY]);
  const first = now(); first.setUTCFullYear(2000);
  assert.equal(now().toISOString().replace(".000Z", "Z"), valid[UTC_KEY]);
  assert.equal(resolveCompanionE2EClockNow({ argv: [SWITCH], env: { ...valid, [UTC_KEY]: "2026-02-31T23:59:58Z" } }), null);
});

test("sidecar environment strips all inherited companion E2E clock values before allowlisted main injection", () => {
  assert.deepEqual(stripCompanionE2EClockEnv({ ...valid, CHRIPTMAS_COMPANION_E2E_CLOCK_SET: "1", KEEP: "yes" }), { KEEP: "yes" });
});

test("sidecar accepts only a complete valid fixed-clock pair", () => {
  const base = {
    CHRIPTMAS_COMPANION_MODE: "packaged",
    CHRIPTMAS_COMPANION_USER_DATA_ROOT: "C:/safe/user-data",
    CHRIPTMAS_COMPANION_REPOSITORY_ROOT: "C:/safe/repository",
    CHRIPTMAS_COMPANION_RESOURCES_ROOT: "C:/safe/resources",
  };
  assert.deepEqual(validateCompanionEnv({ ...base, ...valid }), { ...base, ...valid });
  assert.throws(() => validateCompanionEnv({ ...base, [MODE_KEY]: MODE }), /companion_env_invalid/);
  assert.throws(() => validateCompanionEnv({ ...base, ...valid, [UTC_KEY]: "2026-07-23T23:59:58+08:00" }), /companion_env_invalid/);
});
