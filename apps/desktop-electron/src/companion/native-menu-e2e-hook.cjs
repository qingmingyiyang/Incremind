"use strict";

const TEST_SWITCH = "--chriptmas-e2e-native-menu";
const TEST_ACTION = "companion.pet.hide";
const SHUTDOWN_TEST_SWITCH = "--chriptmas-e2e-native-menu-shutdown";
const SHUTDOWN_TEST_ACTION = "app.quit";
const TEST_ACTION_ENV = "CHRIPTMAS_E2E_NATIVE_MENU_ACTION";

function resolveNativeMenuE2EAction({ argv = process.argv, env = process.env } = {}) {
  if (!Array.isArray(argv) || !env || typeof env !== "object") return null;
  const requestedAction = env[TEST_ACTION_ENV];
  const nativeMenuSelected = argv.includes(TEST_SWITCH);
  const shutdownSelected = argv.includes(SHUTDOWN_TEST_SWITCH);
  if (nativeMenuSelected === shutdownSelected) return null;
  if (shutdownSelected && requestedAction === SHUTDOWN_TEST_ACTION) return SHUTDOWN_TEST_ACTION;
  if (nativeMenuSelected && requestedAction === TEST_ACTION) return TEST_ACTION;
  return null;
}

module.exports = {
  SHUTDOWN_TEST_ACTION,
  SHUTDOWN_TEST_SWITCH,
  TEST_ACTION,
  TEST_ACTION_ENV,
  TEST_SWITCH,
  resolveNativeMenuE2EAction,
};
