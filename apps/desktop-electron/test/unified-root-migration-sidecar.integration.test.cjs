const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");

const { SidecarSupervisor } = require("../src/sidecar-supervisor.cjs");
const {
  RootConfigMigrationController,
  resolveRootConfig,
  verifyCopiedTree,
} = require("../src/vault-root.cjs");

const REPOSITORY_ROOT = path.resolve(__dirname, "..", "..", "..");
const PYTHON = process.env.CHRIPTMAS_OS_PYTHON
  || path.join(REPOSITORY_ROOT, "python-runtime", "python.exe");
const MODULE_ROOT = path.join(REPOSITORY_ROOT, "src");

function requestJson(url, headers) {
  return new Promise((resolve, reject) => {
    const request = http.request(url, { method: "POST", headers, timeout: 5000 }, (response) => {
      let body = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => { body += chunk; });
      response.on("end", () => {
        try { resolve({ status: response.statusCode, body: JSON.parse(body) }); }
        catch { reject(new Error("synthetic_probe_invalid_json")); }
      });
    });
    request.on("error", reject);
    request.on("timeout", () => request.destroy(new Error("synthetic_probe_timeout")));
    request.end();
  });
}

function createSyntheticSqlite(root) {
  const database = path.join(root, ".rebuild-data", "acceptance.sqlite3");
  fs.mkdirSync(path.dirname(database), { recursive: true });
  const result = spawnSync(PYTHON, ["-c", [
    "import sqlite3, sys",
    "db = sqlite3.connect(sys.argv[1])",
    "db.execute('create table acceptance (id integer primary key, value text not null)')",
    "db.execute('insert into acceptance(value) values (?)', ('synthetic-row',))",
    "db.commit()",
    "db.close()",
  ].join("\n"), database], { encoding: "utf8", windowsHide: true });
  assert.equal(result.status, 0, result.stderr);
  return database;
}

function assertSyntheticSqlite(database) {
  const result = spawnSync(PYTHON, ["-c", [
    "import sqlite3, sys",
    "db = sqlite3.connect(sys.argv[1])",
    "assert db.execute('pragma integrity_check').fetchone() == ('ok',)",
    "assert db.execute('select value from acceptance').fetchone() == ('synthetic-row',)",
    "db.close()",
  ].join("\n"), database], { encoding: "utf8", windowsHide: true });
  assert.equal(result.status, 0, result.stderr);
}

function runtimeRoot(root, revision) {
  return { version: 1, revision, roots: { vault: root, model: root, media: root } };
}

function writePointer(userDataDir, root) {
  fs.mkdirSync(userDataDir, { recursive: true });
  fs.writeFileSync(path.join(userDataDir, "data-root.json"), JSON.stringify({
    version: 1, root, roots: { model: root, media: root },
  }));
}

test("synthetic unified-root migration stops the sidecar, preserves SQLite data, and restarts with an authenticated root probe", { timeout: 120000 }, async () => {
  const temporary = path.toNamespacedPath(fs.mkdtempSync(path.join(os.tmpdir(), "u4-m-")));
  const userData = path.join(temporary, "user-data");
  const source = path.join(temporary, "source-vault");
  const target = path.join(temporary, "target-vault");
  let sourceSupervisor = null;
  let targetSupervisor = null;
  try {
    fs.mkdirSync(path.join(source, "config"), { recursive: true });
    fs.copyFileSync(path.join(REPOSITORY_ROOT, "config", "settings.toml.example"), path.join(source, "config", "settings.toml"));
    const sourceDatabase = createSyntheticSqlite(source);
    writePointer(userData, source);

    sourceSupervisor = new SidecarSupervisor({
      rootDir: REPOSITORY_ROOT,
      moduleRoot: MODULE_ROOT,
      workingDir: source,
      runtimeRootConfig: runtimeRoot(source, "synthetic:source"),
      pythonPath: PYTHON,
      startupTimeoutMs: 30000,
    });
    await sourceSupervisor.start();
    await sourceSupervisor.stop();
    assert.equal(sourceSupervisor.child, null, "source sidecar must be offline before migration");

    let quiescenceChecks = 0;
    const migration = new RootConfigMigrationController({
      userDataDir: userData,
      assertSourceQuiescent: () => {
        quiescenceChecks += 1;
        assert.equal(sourceSupervisor.child, null, "migration must not copy while source sidecar is live");
      },
      verifyCopiedTree: (from, to, manifest, roles) => {
        if (!verifyCopiedTree(from, to, manifest, roles)) return false;
        assertSyntheticSqlite(path.join(to, ".rebuild-data", "acceptance.sqlite3"));
        return true;
      },
    });
    const result = migration.migrate({ target: { vaultRoot: target, modelRoot: target, mediaRoot: target } });
    assert.equal(result.status, "switched");
    assert.ok(quiescenceChecks > 0);

    const resolved = resolveRootConfig({ userDataDir: userData });
    assert.equal(resolved.config.vaultRoot, path.resolve(target));
    assertSyntheticSqlite(path.join(target, ".rebuild-data", "acceptance.sqlite3"));
    assertSyntheticSqlite(sourceDatabase);
    assert.equal(fs.existsSync(sourceDatabase), true, "source SQLite must be retained");

    targetSupervisor = new SidecarSupervisor({
      rootDir: REPOSITORY_ROOT,
      moduleRoot: MODULE_ROOT,
      workingDir: target,
      runtimeRootConfig: runtimeRoot(target, "synthetic:target"),
      pythonPath: PYTHON,
      startupTimeoutMs: 30000,
    });
    const session = await targetSupervisor.start();
    const probe = await requestJson(`${session.origin}/api/desktop/runtime-roots/verify`, {
      "X-Chriptmas-Desktop-Session": session.secret,
    });
    assert.equal(probe.status, 200);
    for (const role of ["vault", "model", "media"]) {
      assert.equal(probe.body.roots[role].path, path.resolve(target));
      assert.deepEqual(probe.body.probes[role], { read: true, write: true, cleanup: true });
    }
  } finally {
    await targetSupervisor?.stop();
    await sourceSupervisor?.stop();
    fs.rmSync(temporary, { recursive: true, force: true });
  }
});
