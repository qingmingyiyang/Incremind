const fs = require("node:fs");
const path = require("node:path");
const {
  assertBuildId,
  parseCandidateIdentity,
} = require("../src/candidate-identity.cjs");

const SOURCE_COMMIT = /^[a-f0-9]{40}$/;
const CANDIDATE_EXE_NAME = "Chriptmas OS.exe";

function resolvePath(value) {
  return path.resolve(String(value || ""));
}

function samePath(left, right) {
  return resolvePath(left).toLowerCase() === resolvePath(right).toLowerCase();
}

function hasTraversalSegment(value) {
  return String(value || "").split(/[\\/]+/).includes("..");
}

function safeDirectory(io, directory, label) {
  const entry = io.lstatSync(directory, { throwIfNoEntry: false });
  if (!entry?.isDirectory?.() || entry.isSymbolicLink?.()) {
    throw new Error(`${label} is missing, not a directory, or a symbolic link`);
  }
}

function safeFile(io, file, label) {
  const entry = io.lstatSync(file, { throwIfNoEntry: false });
  if (!entry?.isFile?.() || entry.isSymbolicLink?.()) {
    throw new Error(`${label} is missing, not a regular file, or a symbolic link`);
  }
}

function requireCandidateRequest({ candidateId, sourceCommit }) {
  try {
    assertBuildId(candidateId);
  } catch {
    throw new Error("AI Turn owner-kill alternative candidate ID is invalid");
  }
  if (!SOURCE_COMMIT.test(sourceCommit || "")) {
    throw new Error("AI Turn owner-kill alternative candidate source commit is invalid");
  }
}

function resolveAiTurnKillRecoveryCandidate({
  root,
  defaultExe,
  exe,
  candidateId,
  sourceCommit,
  io = fs,
}) {
  const selectedExe = resolvePath(exe);
  const repositoryDefaultExe = resolvePath(defaultExe);
  if (samePath(selectedExe, repositoryDefaultExe)) {
    return Object.freeze({
      kind: "repository_default",
      exe: repositoryDefaultExe,
      packageRoot: path.dirname(repositoryDefaultExe),
    });
  }

  if (hasTraversalSegment(exe)) {
    throw new Error("AI Turn owner-kill alternative candidate executable must not contain a path traversal segment");
  }
  requireCandidateRequest({ candidateId, sourceCommit });
  const candidateRoot = path.join(resolvePath(root), "release", "candidates", candidateId);
  const unpackedRoot = path.join(candidateRoot, "win-unpacked");
  const expectedExe = path.join(unpackedRoot, CANDIDATE_EXE_NAME);
  if (!samePath(selectedExe, expectedExe)) {
    throw new Error("AI Turn owner-kill alternative candidate executable must be its exact win-unpacked package executable");
  }

  safeDirectory(io, path.join(resolvePath(root), "release"), "candidate release root");
  safeDirectory(io, path.join(resolvePath(root), "release", "candidates"), "candidate collection root");
  safeDirectory(io, candidateRoot, "candidate root");
  safeDirectory(io, unpackedRoot, "candidate unpacked root");
  safeFile(io, expectedExe, "candidate executable");

  const resourcesRoot = path.join(unpackedRoot, "resources");
  const identityPath = path.join(resourcesRoot, "candidate-identity.json");
  safeDirectory(io, resourcesRoot, "candidate resources root");
  safeFile(io, identityPath, "candidate identity file");
  let identity;
  try {
    identity = parseCandidateIdentity(io.readFileSync(identityPath, "utf8"));
  } catch {
    throw new Error("AI Turn owner-kill alternative candidate identity is invalid");
  }
  if (
    identity.candidate_id !== candidateId
    || identity.build_id !== candidateId
    || identity.source_commit !== sourceCommit
    || identity.payload_revision !== `source:${sourceCommit}`
  ) {
    throw new Error("AI Turn owner-kill alternative candidate identity does not match the requested candidate");
  }
  return Object.freeze({
    kind: "pinned_candidate",
    exe: expectedExe,
    packageRoot: unpackedRoot,
    candidateId,
    sourceCommit,
    identity,
  });
}

module.exports = {
  resolveAiTurnKillRecoveryCandidate,
};
