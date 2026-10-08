const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { MAX_MANUAL_BYTES, stageManual } = require("../scripts/stage-manual.cjs");

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-manual-stage-"));
  return { root, repository: path.join(root, "repository"), output: path.join(root, "stage") };
}

test("stages the current root readme as the packaged editable-manual seed", (t) => {
  const current = fixture();
  t.after(() => fs.rmSync(current.root, { recursive: true, force: true }));
  fs.mkdirSync(current.repository, { recursive: true });
  fs.writeFileSync(path.join(current.repository, "readme.md"), "# 使用说明\n\n右键打开菜单。", "utf8");
  const first = stageManual({ repositoryRoot: current.repository, outputRoot: current.output });
  assert.equal(fs.readFileSync(first.target, "utf8"), "# 使用说明\n\n右键打开菜单。");

  fs.writeFileSync(path.join(current.repository, "readme.md"), "# 已更新说明", "utf8");
  const second = stageManual({ repositoryRoot: current.repository, outputRoot: current.output });
  assert.equal(fs.readFileSync(second.target, "utf8"), "# 已更新说明");
});

test("fails closed for missing, oversized, invalid UTF-8 and contaminated stage", (t) => {
  const current = fixture();
  t.after(() => fs.rmSync(current.root, { recursive: true, force: true }));
  fs.mkdirSync(current.repository, { recursive: true });
  assert.throws(() => stageManual({ repositoryRoot: current.repository, outputRoot: current.output }), /missing/);

  fs.writeFileSync(path.join(current.repository, "readme.md"), Buffer.alloc(MAX_MANUAL_BYTES + 1));
  assert.throws(() => stageManual({ repositoryRoot: current.repository, outputRoot: current.output }), /2 MiB/);
  fs.writeFileSync(path.join(current.repository, "readme.md"), Buffer.from([0xc3, 0x28]));
  assert.throws(() => stageManual({ repositoryRoot: current.repository, outputRoot: current.output }), /UTF-8/);

  fs.writeFileSync(path.join(current.repository, "readme.md"), "safe", "utf8");
  fs.mkdirSync(current.output, { recursive: true });
  fs.writeFileSync(path.join(current.output, "unexpected.exe"), "no", "utf8");
  assert.throws(() => stageManual({ repositoryRoot: current.repository, outputRoot: current.output }), /unexpected/);
});
