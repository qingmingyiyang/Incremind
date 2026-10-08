const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const {
  IDENTITY_FILE,
  createCandidateIdentity,
  readRuntimeCandidateIdentity,
  writeCandidateIdentity,
} = require("../src/candidate-identity.cjs");

const buildId = "windows-20260905T010203Z-0123456789ab";
const sourceCommit = "a".repeat(40);

test("candidate identity carries build and committed source provenance", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-candidate-identity-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const identity = createCandidateIdentity({ buildId, sourceCommit, packageVersion: "0.2.0", now: new Date("2026-09-05T01:02:03.000Z") });
  writeCandidateIdentity(root, identity);
  assert.deepEqual(readRuntimeCandidateIdentity({ isPackaged: true, resourcesPath: root }), { status: "ready", identity });
  assert.equal(fs.existsSync(path.join(root, IDENTITY_FILE)), true);
  assert.equal(JSON.stringify(identity).includes(root), false);
});

test("development and malformed packaged identity states cannot claim a candidate passed", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-candidate-identity-state-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  assert.deepEqual(readRuntimeCandidateIdentity({ isPackaged: false, resourcesPath: root }), { status: "not_run", reason: "development_runtime" });
  assert.deepEqual(readRuntimeCandidateIdentity({ isPackaged: true, resourcesPath: root }), { status: "blocked", reason: "candidate_identity_missing_or_unsafe" });
  fs.writeFileSync(path.join(root, IDENTITY_FILE), "{bad", "utf8");
  assert.deepEqual(readRuntimeCandidateIdentity({ isPackaged: true, resourcesPath: root }), { status: "blocked", reason: "candidate_identity_invalid" });
});
