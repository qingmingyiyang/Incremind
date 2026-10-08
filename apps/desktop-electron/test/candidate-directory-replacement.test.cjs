const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  assertDirectoryIdentity,
  beginDirectoryReplacement,
  commitDirectoryReplacement,
  directoryIdentity,
  rollbackDirectoryReplacement,
} = require("../scripts/candidate-directory-replacement.cjs");

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-directory-replacement-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const sourceRoot = path.join(root, "source");
  const targetRoot = path.join(root, "candidate", "frontend-dist");
  fs.mkdirSync(path.join(sourceRoot, "assets"), { recursive: true });
  fs.mkdirSync(path.join(targetRoot, "assets"), { recursive: true });
  fs.writeFileSync(path.join(sourceRoot, "index.html"), "new-index");
  fs.writeFileSync(path.join(sourceRoot, "assets", "app.js"), "new-bytes");
  fs.writeFileSync(path.join(targetRoot, "index.html"), "old-index");
  fs.writeFileSync(path.join(targetRoot, "assets", "app.js"), "old-bytes");
  return {
    root,
    sourceRoot,
    targetRoot,
    stagingRoot: path.join(root, "candidate", ".frontend-stage"),
    backupRoot: path.join(root, "candidate", ".frontend-backup"),
  };
}

test("atomically replaces and commits a candidate directory", (t) => {
  const current = fixture(t);
  const expected = directoryIdentity(current.sourceRoot);
  const transaction = beginDirectoryReplacement(current);

  assert.deepEqual(directoryIdentity(current.targetRoot), expected);
  assert.equal(fs.readFileSync(path.join(current.backupRoot, "index.html"), "utf8"), "old-index");
  commitDirectoryReplacement(transaction);
  assert.equal(fs.existsSync(current.backupRoot), false);
});

test("rolls back the exact previous directory after a later Gate failure", (t) => {
  const current = fixture(t);
  const transaction = beginDirectoryReplacement(current);

  rollbackDirectoryReplacement(transaction);

  assert.equal(fs.readFileSync(path.join(current.targetRoot, "index.html"), "utf8"), "old-index");
  assert.equal(fs.existsSync(current.backupRoot), false);
  assert.equal(fs.existsSync(current.stagingRoot), false);
});

test("detects a same-size mutation after replacement", (t) => {
  const current = fixture(t);
  const transaction = beginDirectoryReplacement(current);
  fs.writeFileSync(path.join(current.targetRoot, "index.html"), "bad-index");

  assert.throws(
    () => assertDirectoryIdentity(current.targetRoot, transaction.expected, "mutated frontend"),
    /content identity changed/,
  );
  rollbackDirectoryReplacement(transaction);
});
