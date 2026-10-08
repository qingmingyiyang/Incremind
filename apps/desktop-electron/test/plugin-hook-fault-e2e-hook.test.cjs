const assert = require("node:assert/strict");
const test = require("node:test");

const { HARNESS_PID_ENV, SWITCH, TOKEN_ENV, resolvePluginHookFaultE2E } = require("../src/plugin-hook-fault-e2e-hook.cjs");

test("plugin Hook fault fixture requires matching bounded token and direct harness parent", () => {
  const token = "p".repeat(32);
  const env = { [TOKEN_ENV]: token, [HARNESS_PID_ENV]: "1234" };
  assert.equal(resolvePluginHookFaultE2E({ argv: ["app", `${SWITCH}=${token}`], env, parentPid: 1234 }), true);
  assert.equal(resolvePluginHookFaultE2E({ argv: ["app"], env, parentPid: 1234 }), false);
  assert.equal(resolvePluginHookFaultE2E({ argv: ["app", `${SWITCH}=${token}`], env: {}, parentPid: 1234 }), false);
  assert.equal(resolvePluginHookFaultE2E({ argv: ["app", `${SWITCH}=${token}`], env, parentPid: 4321 }), false);
  assert.equal(resolvePluginHookFaultE2E({ argv: ["app", `${SWITCH}=short`], env: { [TOKEN_ENV]: "short", [HARNESS_PID_ENV]: "1234" }, parentPid: 1234 }), false);
});
