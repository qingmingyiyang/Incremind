const assert = require("node:assert/strict");
const test = require("node:test");

const { HARNESS_PID_ENV, SWITCH, TOKEN_ENV, resolveMixedMediaE2E } = require("../src/mixed-media-e2e-hook.cjs");

test("mixed media fixture requires the same bounded token through argv and environment", () => {
  const token = "a".repeat(32);
  const env = { [TOKEN_ENV]: token, [HARNESS_PID_ENV]: "1234" };
  assert.equal(resolveMixedMediaE2E({ argv: ["app", `${SWITCH}=${token}`], env, parentPid: 1234 }), true);
  assert.equal(resolveMixedMediaE2E({ argv: ["app"], env, commandLineToken: token, parentPid: 1234 }), true);
  assert.equal(resolveMixedMediaE2E({ argv: ["app"], env, parentPid: 1234 }), false);
  assert.equal(resolveMixedMediaE2E({ argv: ["app", `${SWITCH}=${token}`], env: {}, parentPid: 1234 }), false);
  assert.equal(resolveMixedMediaE2E({ argv: ["app", `${SWITCH}=${token}`, `${SWITCH}=${token}`], env, parentPid: 1234 }), false);
  assert.equal(resolveMixedMediaE2E({ argv: ["app", `${SWITCH}=${token}`], env: { ...env, [TOKEN_ENV]: "b".repeat(32) }, parentPid: 1234 }), false);
  assert.equal(resolveMixedMediaE2E({ argv: ["app", `${SWITCH}=short`], env: { ...env, [TOKEN_ENV]: "short" }, parentPid: 1234 }), false);
  assert.equal(resolveMixedMediaE2E({ argv: ["app", `${SWITCH}=${token}`], env, parentPid: 4321 }), false);
  assert.equal(resolveMixedMediaE2E({ argv: ["app", `${SWITCH}=${token}`], env: { [TOKEN_ENV]: token }, parentPid: 1234 }), false);
});
