"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const {
  SHUTDOWN_TEST_ACTION,
  SHUTDOWN_TEST_SWITCH,
  TEST_ACTION,
  TEST_ACTION_ENV,
  TEST_SWITCH,
  resolveNativeMenuE2EAction,
} = require("../src/companion/native-menu-e2e-hook.cjs");

test("native menu E2E selection hook fails closed unless both test gates select its sole safe action", () => {
  assert.equal(resolveNativeMenuE2EAction({ argv: [], env: { [TEST_ACTION_ENV]: TEST_ACTION } }), null);
  assert.equal(resolveNativeMenuE2EAction({ argv: [TEST_SWITCH], env: {} }), null);
  assert.equal(resolveNativeMenuE2EAction({ argv: [TEST_SWITCH], env: { [TEST_ACTION_ENV]: SHUTDOWN_TEST_ACTION } }), null);
  assert.equal(resolveNativeMenuE2EAction({ argv: [TEST_SWITCH], env: { [TEST_ACTION_ENV]: TEST_ACTION } }), TEST_ACTION);
});

test("shutdown action requires its separate argv gate and exact environment action", () => {
  assert.equal(resolveNativeMenuE2EAction({ argv: [], env: { [TEST_ACTION_ENV]: SHUTDOWN_TEST_ACTION } }), null);
  assert.equal(resolveNativeMenuE2EAction({ argv: [SHUTDOWN_TEST_SWITCH], env: {} }), null);
  assert.equal(resolveNativeMenuE2EAction({ argv: [SHUTDOWN_TEST_SWITCH], env: { [TEST_ACTION_ENV]: TEST_ACTION } }), null);
  assert.equal(resolveNativeMenuE2EAction({ argv: [SHUTDOWN_TEST_SWITCH], env: { [TEST_ACTION_ENV]: SHUTDOWN_TEST_ACTION } }), SHUTDOWN_TEST_ACTION);
  assert.equal(resolveNativeMenuE2EAction({ argv: [TEST_SWITCH, SHUTDOWN_TEST_SWITCH], env: { [TEST_ACTION_ENV]: TEST_ACTION } }), null);
});
