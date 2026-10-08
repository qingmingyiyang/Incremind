const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { createCandidateIdentity } = require("../src/candidate-identity.cjs");
const { resolveAiTurnKillRecoveryCandidate } = require("../scripts/fixed-candidate-binding.cjs");

const candidateId = "windows-20260906T051535Z-af4e8fa2a0cf";
const sourceCommit = "af4e8fa2a0cfcfff813f127bd69aa218d8effae2";

function stageCandidate(root) {
  const unpacked = path.join(root, "release", "candidates", candidateId, "win-unpacked");
  const resources = path.join(unpacked, "resources");
  fs.mkdirSync(resources, { recursive: true });
  const exe = path.join(unpacked, "Chriptmas OS.exe");
  fs.writeFileSync(exe, "candidate executable");
  fs.writeFileSync(path.join(resources, "candidate-identity.json"), `${JSON.stringify(createCandidateIdentity({
    buildId: candidateId,
    sourceCommit,
    packageVersion: "0.2.0",
    now: new Date("2026-09-06T05:15:35.629Z"),
  }))}\n`);
  return exe;
}

test("AI Turn owner-kill candidate binding retains the repository default and admits only an exact pinned candidate", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-kill-candidate-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const defaultExe = path.join(root, "release", "win-unpacked", "Chriptmas OS.exe");
  const candidateExe = stageCandidate(root);
  assert.equal(resolveAiTurnKillRecoveryCandidate({ root, defaultExe, exe: defaultExe }).kind, "repository_default");
  const selected = resolveAiTurnKillRecoveryCandidate({ root, defaultExe, exe: candidateExe, candidateId, sourceCommit });
  assert.deepEqual({ kind: selected.kind, candidateId: selected.candidateId, sourceCommit: selected.sourceCommit, exe: selected.exe }, {
    kind: "pinned_candidate", candidateId, sourceCommit, exe: candidateExe,
  });
});

test("AI Turn owner-kill candidate binding rejects unpinned, traversing, mismatched, and symbolic-link package paths", () => {
  const root = path.resolve("C:/candidate-root");
  const defaultExe = path.join(root, "release", "win-unpacked", "Chriptmas OS.exe");
  const candidateExe = path.join(root, "release", "candidates", candidateId, "win-unpacked", "Chriptmas OS.exe");
  const files = new Map([
    [path.join(root, "release"), "directory"],
    [path.join(root, "release", "candidates"), "directory"],
    [path.join(root, "release", "candidates", candidateId), "directory"],
    [path.join(root, "release", "candidates", candidateId, "win-unpacked"), "directory"],
    [candidateExe, "file"],
    [path.join(root, "release", "candidates", candidateId, "win-unpacked", "resources"), "directory"],
    [path.join(root, "release", "candidates", candidateId, "win-unpacked", "resources", "candidate-identity.json"), "file"],
  ]);
  const identity = createCandidateIdentity({ buildId: candidateId, sourceCommit, packageVersion: "0.2.0", now: new Date("2026-09-06T05:15:35.629Z") });
  const io = {
    lstatSync(file) {
      const type = files.get(file);
      if (!type) return undefined;
      return { isDirectory: () => type === "directory", isFile: () => type === "file", isSymbolicLink: () => type === "link" };
    },
    readFileSync() { return JSON.stringify(identity); },
  };
  assert.throws(() => resolveAiTurnKillRecoveryCandidate({ root, defaultExe, exe: candidateExe }), /candidate ID is invalid/);
  assert.throws(() => resolveAiTurnKillRecoveryCandidate({ root, defaultExe, exe: candidateExe.replace("win-unpacked", "win-unpacked\\..\\win-unpacked"), candidateId, sourceCommit, io }), /path traversal/);
  assert.throws(() => resolveAiTurnKillRecoveryCandidate({ root, defaultExe, exe: candidateExe, candidateId, sourceCommit: "b".repeat(40), io }), /identity does not match/);
  files.set(candidateExe, "link");
  assert.throws(() => resolveAiTurnKillRecoveryCandidate({ root, defaultExe, exe: candidateExe, candidateId, sourceCommit, io }), /candidate executable.*symbolic link/);
});
