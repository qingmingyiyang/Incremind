const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const test = require("node:test");

const { VaultRecoveryController } = require("../src/vault-recovery-controller.cjs");

function childWith({ code = 0, stdout = "", stderr = "" } = {}) {
  const child = new EventEmitter();
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  child.stdout.setEncoding = () => {};
  child.stderr.setEncoding = () => {};
  process.nextTick(() => {
    if (stdout) child.stdout.emit("data", stdout);
    if (stderr) child.stderr.emit("data", stderr);
    child.emit("exit", code);
  });
  return child;
}

test("runs only while sidecar is offline and passes bounded CLI arguments", async () => {
  const calls = [];
  const controller = new VaultRecoveryController({
    pythonPath: "python.exe",
    moduleRoot: "C:\\app\\sidecar",
    workingRoot: "C:\\data\\vault",
    sidecarOffline: () => true,
    spawnChild: (...args) => {
      calls.push(args);
      return childWith({ stdout: '{"status":"adopted","operation_id":"restore-1","file_count":3}\n' });
    },
  });
  const result = await controller.adopt("restore-1");
  assert.equal(result.status, "adopted");
  assert.deepEqual(calls[0][1], [
    "-m", "backend.vault_recovery_cli", "--working-root", "C:\\data\\vault",
    "--operation-id", "restore-1",
  ]);
  assert.equal(calls[0][2].windowsHide, true);
  assert.deepEqual(calls[0][2].stdio, ["ignore", "pipe", "pipe"]);
});

test("rejects active sidecar invalid IDs and failed offline worker", async () => {
  const active = new VaultRecoveryController({
    pythonPath: "python", moduleRoot: "src", workingRoot: "vault",
    sidecarOffline: () => false, spawnChild: () => { throw new Error("must not spawn"); },
  });
  await assert.rejects(active.recoverPending(), /must_be_offline/);
  await assert.rejects(active.adopt("../escape"), /operation_invalid/);

  const failed = new VaultRecoveryController({
    pythonPath: "python", moduleRoot: "src", workingRoot: "vault",
    sidecarOffline: () => true,
    spawnChild: () => childWith({ code: 2, stderr: '{"status":"failed","error":"VaultOperationalRecoveryConflict"}\n' }),
  });
  await assert.rejects(failed.adopt("restore-safe"), /VaultOperationalRecoveryConflict/);
});

test("startup recovery uses the same offline worker without an operation id", async () => {
  let command = null;
  const controller = new VaultRecoveryController({
    pythonPath: "python", moduleRoot: "src", workingRoot: "vault",
    sidecarOffline: () => true,
    spawnChild: (_python, args) => {
      command = args;
      return childWith({ stdout: '{"status":"reconciled","operations":[]}\n' });
    },
  });
  assert.equal((await controller.recoverPending()).status, "reconciled");
  assert.deepEqual(command.slice(-1), ["--recover-pending"]);
});
