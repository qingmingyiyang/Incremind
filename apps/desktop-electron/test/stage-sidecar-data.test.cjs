const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-tracked-stage-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return root;
}

function git(root, args) {
  const result = spawnSync("git", args, { cwd: root, encoding: "utf8", windowsHide: true });
  assert.equal(result.error, undefined);
  assert.equal(result.status, 0, result.stderr);
}

test("copies only tracked application files from a genuine temporary Git index", (t) => {
  const root = fixture(t);
  const source = path.join(root, "src", "backend");
  const stage = path.join(root, ".sidecar-stage");
  const target = path.join(stage, "backend");
  fs.mkdirSync(path.join(source, "nested"), { recursive: true });
  fs.writeFileSync(path.join(root, ".gitignore"), "*.sqlite3\n*.pyc\n");
  fs.writeFileSync(path.join(source, "nested", "app.py"), "print('synthetic')\n");
  git(root, ["init", "--quiet"]);
  git(root, ["add", ".gitignore", "src/backend/nested/app.py"]);
  fs.writeFileSync(path.join(source, ".env"), "synthetic untracked");
  fs.writeFileSync(path.join(source, "ignored.sqlite3"), "synthetic ignored");
  fs.writeFileSync(path.join(source, "nested", "ignored.pyc"), "synthetic bytecode");
  const { collectApplicationSourceEntries, copy, listTrackedApplicationFiles } = require("../scripts/stage-sidecar.cjs");
  const entries = collectApplicationSourceEntries([[source, target, true]], fs, stage, { projectRoot: root });
  assert.deepEqual(entries.map((entry) => entry.path), ["backend/nested/app.py"]);
  copy(source, target, true, listTrackedApplicationFiles(root));
  assert.equal(fs.readFileSync(path.join(target, "nested", "app.py"), "utf8"), "print('synthetic')\n");
  for (const relative of [".env", "ignored.sqlite3", "nested/ignored.pyc"]) {
    assert.equal(fs.existsSync(path.join(target, relative)), false);
  }
});

test("a missing Git executable fails application enumeration before copying", (t) => {
  const root = fixture(t);
  const { listTrackedApplicationFiles } = require("../scripts/stage-sidecar.cjs");
  const result = spawnSync(path.join(root, "missing-git.exe"), ["ls-files"], { cwd: root });
  assert.equal(result.error.code, "ENOENT");
  assert.throws(() => listTrackedApplicationFiles(root, () => result), /Git is required/);
  assert.equal(fs.existsSync(path.join(root, ".sidecar-stage")), false);
});

test("runtime source defaults to a separate portable Python directory", () => {
  const { RUNTIME_SOURCE, RUNTIME_TARGET } = require("../scripts/stage-sidecar.cjs");
  const projectRoot = path.resolve(__dirname, "../../..");
  assert.equal(RUNTIME_SOURCE, path.resolve(process.env.CHRIPTMAS_SIDECAR_PYTHON_RUNTIME || path.join(projectRoot, "python-runtime")));
  assert.equal(RUNTIME_TARGET, path.resolve(__dirname, "../.sidecar-stage/runtime"));
});

test("verify-build rejects injected package data and clears only its owned stage", (t) => {
  const root = fixture(t);
  const resources = path.join(root, "release", "win-unpacked", "resources");
  const stage = path.join(root, ".sidecar-stage");
  fs.mkdirSync(path.join(resources, "sidecar", "runtime"), { recursive: true });
  fs.mkdirSync(stage);
  fs.writeFileSync(path.join(stage, "keep.txt"), "synthetic stage");
  fs.writeFileSync(path.join(resources, "sidecar", "runtime", "python.exe"), "synthetic Python");
  fs.writeFileSync(path.join(resources, "injected.sqlite3"), "synthetic database");
  const { verifyPackageData } = require("../scripts/verify-build.cjs");
  assert.throws(() => verifyPackageData(resources, stage), /package_data_forbidden: injected\.sqlite3/);
  assert.equal(fs.existsSync(stage), false);
  assert.equal(fs.existsSync(path.join(resources, "injected.sqlite3")), true);
});

function write(root, relative, body = "synthetic") {
  const target = path.join(root, relative);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, body);
  return target;
}

function isolatedScripts(t) {
  const root = fixture(t);
  const electron = path.join(root, "apps", "desktop-electron");
  const modules = [
    "scripts/stage-sidecar.cjs", "scripts/verify-sidecar.cjs", "scripts/package-guard.cjs",
    "scripts/verify-build.cjs", "scripts/smoke-packaged.cjs", "scripts/stage-manual.cjs",
    "scripts/sidecar-transfer.cjs", "src/candidate-identity.cjs",
  ];
  for (const relative of modules) {
    write(electron, relative, fs.readFileSync(path.join(__dirname, "..", relative)));
  }
  return { root, electron, stage: path.join(electron, ".sidecar-stage") };
}

function runScript(value, name, args = [], environment = {}) {
  const result = spawnSync(process.execPath, [path.join(value.electron, "scripts", name), ...args], {
    cwd: value.root, encoding: "utf8", windowsHide: true, timeout: 30000,
    env: { ...process.env, CHRIPTMAS_SIDECAR_PYTHON_RUNTIME: path.join(value.root, "python-runtime"), ...environment },
  });
  assert.equal(result.error, undefined);
  assert.equal(result.signal, null);
  return result;
}

for (const contamination of [true, false]) {
  test(`the real staging CLI clears its stage before copying when ${contamination ? "runtime contains user data" : "Python is missing"}`, (t) => {
    const value = isolatedScripts(t);
    write(value.stage, "stale.txt");
    fs.mkdirSync(path.join(value.root, "python-runtime"));
    if (contamination) {
      for (const relative of ["python.exe", "secrets.json", "recognition.sqlite3", ".env", "workspace/input.txt"]) {
        write(value.root, `python-runtime/${relative}`);
      }
    }
    const result = runScript(value, "stage-sidecar.cjs", ["--clean"]);
    assert.equal(result.status, 1);
    assert.match(result.stderr, contamination ? /package_data_forbidden/ : /python-runtime.*python\.exe/);
    assert.equal(result.stdout.includes("runtime staged"), false);
    assert.equal(result.stdout.includes("cache miss"), false);
    assert.equal(fs.existsSync(value.stage), false);
  });
}

test("the real staging CLI rejects tracked data after copying and before compiling", (t) => {
  const value = isolatedScripts(t);
  write(value.root, "python-runtime/python.exe");
  for (const relative of ["src/backend/app.py", "src/backend/injected.db", "src/core/app.py", "src/rebuild/app.py",
    "config/rebuild.toml.example", "config/settings.toml.example", "config/codex-hooks.toml", "requirements.txt",
    "tools/scripts/verify-runtime-dependencies.py"]) write(value.root, relative);
  git(value.root, ["init", "--quiet"]);
  git(value.root, ["add", "src/backend", "src/core", "src/rebuild"]);
  const result = runScript(value, "stage-sidecar.cjs", ["--clean"]);
  assert.equal(result.status, 1);
  assert.match(result.stdout, /runtime staged via/);
  assert.match(result.stderr, /package_data_forbidden: backend\/injected\.db/);
  assert.equal(result.stdout.includes("application import and bytecode"), false);
  assert.equal(fs.existsSync(value.stage), false);
  assert.equal(fs.existsSync(path.join(value.root, "src", "backend", "injected.db")), true);
});

test("the real build verifier checks all resources before sidecar or manual verification", (t) => {
  const value = isolatedScripts(t);
  write(value.electron, "frontend-dist/index.html", '<!doctype html><html><script src="./assets/app.js"></script></html>');
  write(value.electron, "frontend-dist/assets/app.js", "console.log('synthetic');\n");
  write(value.electron, "package.json", JSON.stringify({ version: "0.2.0" }));
  write(value.electron, "release/win-unpacked/resources/injected.sqlite3");
  write(value.stage, "stale.txt");
  const result = runScript(value, "verify-build.cjs");
  assert.equal(result.status, 1);
  assert.match(result.stderr, /package_data_forbidden: injected\.sqlite3/);
  assert.equal(result.stderr.includes("manual"), false);
  assert.equal(result.stdout.includes("sidecar verified"), false);
  assert.equal(fs.existsSync(value.stage), false);
  assert.equal(fs.existsSync(path.join(value.electron, "release", "win-unpacked", "resources", "injected.sqlite3")), true);
});

test("changing an actual guard rule invalidates runtime and application cache policies", async (t) => {
  const value = isolatedScripts(t);
  const script = path.join(value.electron, "scripts", "stage-sidecar.cjs");
  const old = require(script);
  const runtimeSource = path.join(value.root, "python-runtime");
  write(runtimeSource, "python.exe");
  write(value.stage, "runtime/python.exe");
  write(value.root, "src/backend/app.py");
  write(value.stage, "backend/app.py");
  write(value.stage, "backend/__pycache__/app.pyc");
  git(value.root, ["init", "--quiet"]);
  git(value.root, ["add", "src/backend/app.py"]);
  const inputs = [[path.join(value.root, "src", "backend"), path.join(value.stage, "backend"), true]];
  const snapshot = old.snapshotRuntimeSource(runtimeSource);
  const runtimeFiles = [{ path: "runtime/python.exe", size: 9, sha256: old.sha256(path.join(value.stage, "runtime", "python.exe")) }];
  const applicationSnapshot = await old.snapshotApplicationSources(inputs, { stageRoot: value.stage });
  const applicationFiles = ["backend/__pycache__/app.pyc", "backend/app.py"].map((relative) => ({
    path: relative, size: 9, sha256: old.sha256(path.join(value.stage, ...relative.split("/"))),
  }));
  const cachePath = path.join(value.electron, ".cache", "runtime.json");
  const runtimeProofOptions = { stageRoot: value.stage, proofRoot: path.join(value.electron, ".cache", "runtime-proofs") };
  const applicationProofOptions = { stageRoot: value.stage, proofRoot: path.join(value.electron, ".cache", "application-proofs") };
  old.writeRuntimeHashCache(snapshot, runtimeFiles, { cachePath });
  old.writeStageReuseProof(snapshot, runtimeFiles, runtimeProofOptions);
  old.writeApplicationStageProof(applicationSnapshot, applicationFiles, 123, applicationProofOptions);
  assert.equal(old.readRuntimeHashCache(snapshot, { cachePath }).hit, true);
  assert.equal(old.readStageReuseProof(snapshot, runtimeFiles, runtimeProofOptions).hit, true);
  assert.equal(old.readApplicationStageProof(applicationSnapshot, applicationProofOptions).hit, true);
  const guardPath = path.join(value.electron, "scripts", "package-guard.cjs");
  const guardSource = fs.readFileSync(guardPath, "utf8");
  const rule = '"backups", "logs", "data"';
  assert.equal(guardSource.split(rule).length, 2);
  const guardRoot = path.join(value.root, "guard-check");
  fs.mkdirSync(path.join(guardRoot, "private-data"), { recursive: true });
  assert.doesNotThrow(() => require(guardPath).assertPackageClean(guardRoot, { runtimeRoots: [guardRoot] }));
  fs.writeFileSync(guardPath, guardSource.replace(rule, '"backups", "logs", "data", "private-data"'));
  delete require.cache[require.resolve(guardPath)];
  delete require.cache[require.resolve(script)];
  const current = require(script);
  assert.throws(() => require(guardPath).assertPackageClean(guardRoot, { runtimeRoots: [guardRoot] }), /package_data_forbidden: private-data/);
  assert.notEqual(current.RUNTIME_HASH_POLICY, old.RUNTIME_HASH_POLICY);
  assert.notEqual(current.APPLICATION_STAGE_POLICY, old.APPLICATION_STAGE_POLICY);
  assert.equal(current.readRuntimeHashCache(snapshot, { cachePath }).reason, "cache_mismatch");
  assert.equal(current.readStageReuseProof(snapshot, runtimeFiles, runtimeProofOptions).reason, "stage_proof_mismatch");
  assert.equal(current.readApplicationStageProof(applicationSnapshot, applicationProofOptions).reason, "application_proof_mismatch");
});
