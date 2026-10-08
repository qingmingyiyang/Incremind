const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-package-guard-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return root;
}

function file(root, relative) {
  const target = path.join(root, relative);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, "synthetic");
}

for (const relative of [
  "secrets.json", "nested/SECRETS.JSON", ".env", "pkg/.env.test", "data-root.json",
  "recognition.sqlite", "nested/recognition.sqlite3", "nested/data.DB", "data.sqlite3-wal", "data.sqlite3-shm",
]) {
  test(`rejects forbidden package file ${relative} without reading its body`, (t) => {
    const root = fixture(t);
    file(root, relative);
    const { assertPackageClean } = require("../scripts/package-guard.cjs");
    assert.throws(() => assertPackageClean(root), (error) => {
      assert.equal(error.code, "package_data_forbidden");
      assert.deepEqual(error.paths, [relative]);
      assert.equal(error.message.includes("synthetic"), false);
      return true;
    });
  });
}

for (const relative of [".rebuild-data", "workspace", "backups", "logs", "data", "WORKSPACE"]) {
  test(`rejects runtime top-level directory ${relative}`, (t) => {
    const root = fixture(t);
    fs.mkdirSync(path.join(root, relative));
    const { assertPackageClean } = require("../scripts/package-guard.cjs");
    assert.throws(() => assertPackageClean(root, { runtimeRoots: [root] }), /package_data_forbidden/);
  });
}

test("runtime-only markers are limited to their declared root", (t) => {
  const root = fixture(t);
  file(root, "runtime/python.exe");
  file(root, "runtime/Library/bin/library.dll");
  file(root, "runtime/Lib/site-packages/pkg/data/readme.txt");
  file(root, "app/logs/documentation.txt");
  file(root, "config/settings.toml");
  const { assertPackageClean } = require("../scripts/package-guard.cjs");
  assert.doesNotThrow(() => assertPackageClean(root, { runtimeRoots: [path.join(root, "runtime")] }));
  file(root, "runtime/config/settings.toml");
  assert.throws(() => assertPackageClean(root, { runtimeRoots: [path.join(root, "runtime")] }), (error) => {
    assert.deepEqual(error.paths, ["runtime/config/settings.toml"]);
    return true;
  });
});

test("an explicit allowlist permits only the exact listed file", (t) => {
  const root = fixture(t);
  file(root, "Lib/site-packages/pkg/example.db");
  const { assertPackageClean } = require("../scripts/package-guard.cjs");
  const allowlist = ["Lib/site-packages/pkg/example.db"];
  assert.doesNotThrow(() => assertPackageClean(root, { allowlist }));
  file(root, "elsewhere/example.db");
  assert.throws(() => assertPackageClean(root, { allowlist }), (error) => {
    assert.deepEqual(error.paths, ["elsewhere/example.db"]);
    return true;
  });
  assert.throws(() => assertPackageClean(root, { allowlist: ["**/*.db"] }), /package_allowlist_invalid/);
  assert.throws(() => assertPackageClean(root, { allowlist: ["../example.db"] }), /package_allowlist_invalid/);
});

test("reports all forbidden relative paths in a stable order", (t) => {
  const root = fixture(t);
  file(root, "z/data.db");
  file(root, ".env");
  file(root, "secrets.json");
  const { assertPackageClean } = require("../scripts/package-guard.cjs");
  assert.throws(() => assertPackageClean(root), (error) => {
    assert.deepEqual(error.paths, [".env", "secrets.json", "z/data.db"]);
    return true;
  });
});

test("Python preflight rejects missing directories and executables before any stage exists", (t) => {
  const root = fixture(t);
  const runtime = path.join(root, "python-runtime");
  const { assertPythonRuntime } = require("../scripts/package-guard.cjs");
  assert.throws(() => assertPythonRuntime(runtime), /python-runtime.*python\.exe/);
  fs.mkdirSync(runtime);
  assert.throws(() => assertPythonRuntime(runtime), /python-runtime.*python\.exe/);
  assert.equal(fs.existsSync(path.join(root, ".sidecar-stage")), false);
  file(runtime, "python.exe");
  assert.doesNotThrow(() => assertPythonRuntime(runtime));
});

test("Python preflight rejects a contaminated synthetic runtime before copying", (t) => {
  const root = fixture(t);
  const runtime = path.join(root, "python-runtime");
  for (const relative of ["python.exe", "secrets.json", "recognition.sqlite3", ".env", "workspace/input.txt"]) file(runtime, relative);
  const { assertPythonRuntime } = require("../scripts/package-guard.cjs");
  assert.throws(() => assertPythonRuntime(runtime), (error) => {
    assert.deepEqual(error.paths, [".env", "recognition.sqlite3", "secrets.json", "workspace"]);
    return true;
  });
  assert.equal(fs.existsSync(path.join(root, ".sidecar-stage")), false);
});

for (const location of [".sidecar-stage", "release/win-unpacked/resources"]) {
  test(`guard failure at ${location} removes only the explicitly owned stage`, (t) => {
    const root = fixture(t);
    const stageRoot = path.join(root, ".sidecar-stage");
    file(stageRoot, "runtime/python.exe");
    file(root, "runtime/keep.txt");
    const guardedRoot = path.join(root, location);
    file(guardedRoot, "injected.db");
    const { assertPackageClean } = require("../scripts/package-guard.cjs");
    assert.throws(() => assertPackageClean(guardedRoot, { cleanupStageRoot: stageRoot }), /package_data_forbidden/);
    assert.equal(fs.existsSync(stageRoot), false);
    assert.equal(fs.readFileSync(path.join(root, "runtime/keep.txt"), "utf8"), "synthetic");
    if (location.startsWith("release")) assert.equal(fs.existsSync(path.join(guardedRoot, "injected.db")), true);
  });
}

test("invalid cleanup targets and runtime roots fail closed", (t) => {
  const root = fixture(t);
  const { assertPackageClean } = require("../scripts/package-guard.cjs");
  assert.throws(() => assertPackageClean(root, { runtimeRoots: [path.join(root, "../outside")] }), /package_runtime_root_invalid/);
  assert.throws(() => assertPackageClean(root, { cleanupStageRoot: path.join(root, "runtime") }), /package_stage_root_invalid/);
});

test("matches Windows runtime directory aliases without losing top-level data markers", (t) => {
  const root = fixture(t);
  file(root, "sidecar/Runtime/Logs/entry.txt");
  file(root, "sidecar/Runtime/CONFIG/settings.TOML");
  const { assertPackageClean } = require("../scripts/package-guard.cjs");
  assert.throws(() => assertPackageClean(root, { runtimeRoots: [path.join(root, "sidecar", "runtime")] }), (error) => {
    assert.deepEqual(error.paths, ["sidecar/Runtime/CONFIG/settings.TOML", "sidecar/Runtime/Logs"]);
    return true;
  });
});
