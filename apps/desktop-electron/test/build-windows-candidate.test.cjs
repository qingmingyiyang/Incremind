const assert = require("node:assert/strict");
const test = require("node:test");
const {
  candidateE2EEnvironment,
  candidatePaths,
  runNpm,
} = require("../scripts/build-windows-candidate.cjs");

test("candidate gate E2E runs the freshly isolated executable", () => {
  const candidate = candidatePaths("windows-20260906T120000Z-aaaaaaaaaaaa");
  const environment = candidateE2EEnvironment(candidate, {
    CHRIPTMAS_E2E_EXE: "F:\\historical\\win-unpacked\\Chriptmas OS.exe",
    PRESERVED_VALUE: "preserved",
  });
  let invocation;

  runNpm("test candidate E2E", ["run", "test:e2e:electron"], "F:\\candidate-cwd", environment, {
    platform: "win32",
    comSpec: "C:\\Windows\\System32\\cmd.exe",
    spawn(command, args, options) {
      invocation = { command, args, options };
      return { status: 0 };
    },
  });

  assert.equal(environment.CHRIPTMAS_E2E_EXE, candidate.executable);
  assert.equal(environment.PRESERVED_VALUE, "preserved");
  assert.equal(invocation.command, "C:\\Windows\\System32\\cmd.exe");
  assert.deepEqual(invocation.args, ["/d", "/s", "/c", "npm.cmd run test:e2e:electron"]);
  assert.equal(invocation.options.cwd, "F:\\candidate-cwd");
  assert.equal(invocation.options.env, environment);
  assert.equal(invocation.options.env.CHRIPTMAS_E2E_EXE, candidate.executable);
});