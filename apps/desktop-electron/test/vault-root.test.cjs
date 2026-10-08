const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const { resolvePackagedVaultRoot } = require("../src/vault-root.cjs");

function existsFor(paths) {
  return (candidate) => paths.has(candidate);
}

test("new packaged installs select a formal vault below userData", () => {
  const userDataDir = path.join("C:", "Users", "Test", "AppData", "Roaming", "Chriptmas OS");
  const result = resolvePackagedVaultRoot({ userDataDir, exists: existsFor(new Set()) });

  assert.equal(result.mode, "fresh_vault");
  assert.equal(result.workingDir, path.join(path.resolve(userDataDir), "vault"));
});

test("legacy packaged data stays at its current root until an explicit migration", () => {
  const userDataDir = path.join("C:", "Users", "Test", "AppData", "Roaming", "Chriptmas OS");
  const root = path.resolve(userDataDir);
  const result = resolvePackagedVaultRoot({
    userDataDir,
    exists: existsFor(new Set([path.join(root, ".rebuild-data")])),
  });

  assert.equal(result.mode, "legacy");
  assert.equal(result.workingDir, root);
});

test("two populated roots fail closed instead of choosing or moving data", () => {
  const userDataDir = path.join("C:", "Users", "Test", "AppData", "Roaming", "Chriptmas OS");
  const root = path.resolve(userDataDir);
  const vault = path.join(root, "vault");
  const result = resolvePackagedVaultRoot({
    userDataDir,
    exists: existsFor(new Set([path.join(root, "library"), path.join(vault, "data")])),
  });

  assert.deepEqual(result, { mode: "conflict", workingDir: null });
});

test("packaged installs honor an absolute configured project data root", () => {
  const userDataDir = path.join("C:", "Users", "Test", "AppData", "Roaming", "Chriptmas OS");
  const pointer = path.join(path.resolve(userDataDir), "data-root.json");
  const projectRoot = path.resolve("F:\\Chriptmas_OS");
  const result = resolvePackagedVaultRoot({
    userDataDir,
    exists: existsFor(new Set([pointer])),
    readFile: () => JSON.stringify({ root: projectRoot }),
  });

  assert.deepEqual(result, { mode: "configured", workingDir: projectRoot, pointerPath: pointer });
});

test("configured data root fails closed for malformed or relative pointers", () => {
  const userDataDir = path.join("C:", "Users", "Test", "AppData", "Roaming", "Chriptmas OS");
  const pointer = path.join(path.resolve(userDataDir), "data-root.json");
  const options = { userDataDir, exists: existsFor(new Set([pointer])) };

  assert.throws(
    () => resolvePackagedVaultRoot({ ...options, readFile: () => "{" }),
    /vault_root_pointer_invalid/,
  );
  assert.throws(
    () => resolvePackagedVaultRoot({ ...options, readFile: () => JSON.stringify({ root: "data" }) }),
    /vault_root_pointer_invalid/,
  );
});
