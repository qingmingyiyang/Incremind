const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { resolvePackagedAppLayout } = require("../scripts/smoke-packaged.cjs");

function temporaryResources() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-smoke-layout-"));
}

function writeSupervisor(resourcesRoot) {
  const supervisorPath = path.join(resourcesRoot, "app", "src", "sidecar-supervisor.cjs");
  fs.mkdirSync(path.dirname(supervisorPath), { recursive: true });
  fs.writeFileSync(supervisorPath, "module.exports = {};\n");
  return supervisorPath;
}

test("accepts the formal no-asar packaged app directory", () => {
  const resourcesRoot = temporaryResources();
  try {
    const supervisorPath = writeSupervisor(resourcesRoot);
    assert.deepEqual(resolvePackagedAppLayout(resourcesRoot), {
      appRoot: path.join(resourcesRoot, "app"),
      supervisorPath,
    });
  } finally {
    fs.rmSync(resourcesRoot, { recursive: true, force: true });
  }
});

test("rejects a legacy asar payload even when an app directory exists", () => {
  const resourcesRoot = temporaryResources();
  try {
    writeSupervisor(resourcesRoot);
    fs.writeFileSync(path.join(resourcesRoot, "app.asar"), "legacy");
    assert.throws(
      () => resolvePackagedAppLayout(resourcesRoot),
      /app\.asar is not allowed/,
    );
  } finally {
    fs.rmSync(resourcesRoot, { recursive: true, force: true });
  }
});

test("rejects a missing app payload or supervisor", () => {
  const resourcesRoot = temporaryResources();
  try {
    assert.throws(() => resolvePackagedAppLayout(resourcesRoot), /app directory is missing/);
    fs.mkdirSync(path.join(resourcesRoot, "app"));
    assert.throws(() => resolvePackagedAppLayout(resourcesRoot), /sidecar supervisor is missing/);
  } finally {
    fs.rmSync(resourcesRoot, { recursive: true, force: true });
  }
});
