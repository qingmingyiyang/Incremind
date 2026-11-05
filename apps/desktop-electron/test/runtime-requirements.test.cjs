const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
  INPUTS, compileRuntimeRequirements, snapshotApplicationSources, snapshotApplicationStage,
} = require("../scripts/stage-sidecar.cjs");

const projectRoot = path.resolve(__dirname, "..", "..", "..");

test("stages actual nested declarations and includes flat output in the application inventory", async () => {
  const work = path.join(projectRoot, "work", "R7-full-followup");
  fs.mkdirSync(work, { recursive: true });
  const folder = fs.mkdtempSync(path.join(work, "requirements-stage-"));
  const source = path.join(folder, "source");
  const stage = path.join(folder, "stage");
  fs.mkdirSync(source);
  fs.mkdirSync(stage);
  const names = ["requirements.txt", "requirements-web.txt", "requirements-memory.txt", "verify-runtime-dependencies.py"];
  const inputs = names.map((name) => {
    const input = INPUTS.find(([, target]) => path.basename(target) === name);
    assert.ok(input, `missing staged input ${name}`);
    const sourceFile = path.join(source, name);
    const targetFile = path.join(stage, name);
    fs.copyFileSync(input[0], sourceFile);
    fs.copyFileSync(sourceFile, targetFile);
    return [sourceFile, targetFile, true];
  });
  const before = await snapshotApplicationSources(inputs, { stageRoot: stage });
  assert.match(compileRuntimeRequirements(stage), /compiled runtime requirements: \d+ requirements/);
  const flat = fs.readFileSync(path.join(stage, "runtime-requirements.txt"), "utf8");
  assert.match(flat, /sqlite-vec==0\.1\.9/);
  assert.match(flat, /fastapi/);
  assert.match(flat, /faster-whisper/);
  assert.doesNotMatch(flat, /^-r /m);
  assert.ok(snapshotApplicationStage(stage).files.some((file) => file.path === "runtime-requirements.txt"));
  fs.appendFileSync(path.join(source, "requirements-memory.txt"), "\nqrcode>=8.2,<9\n");
  const changed = await snapshotApplicationSources(inputs, { stageRoot: stage });
  assert.notEqual(changed.content_set_sha256, before.content_set_sha256);
  fs.copyFileSync(path.join(source, "requirements-memory.txt"), path.join(stage, "requirements-memory.txt"));
  compileRuntimeRequirements(stage);
  assert.equal(fs.readFileSync(path.join(stage, "runtime-requirements.txt"), "utf8").split("qrcode").length - 1, 2);
});

test("fails explicitly when the configured build interpreter is unavailable", () => {
  assert.throws(() => compileRuntimeRequirements(projectRoot, {
    buildPython: path.join(projectRoot, "work", "R7-full-followup", "nonexistent-python.exe"),
  }), /ENOENT/);
});
