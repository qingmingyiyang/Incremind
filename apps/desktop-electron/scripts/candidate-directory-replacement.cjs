const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

function requireDirectory(directory, label, io = fs) {
  const stat = io.lstatSync(directory, { throwIfNoEntry: false });
  if (!stat?.isDirectory() || stat.isSymbolicLink()) {
    throw new Error(`${label} must be a regular non-symlink directory`);
  }
  return stat;
}

function sha256File(filePath, io = fs) {
  return crypto.createHash("sha256").update(io.readFileSync(filePath)).digest("hex");
}

function directoryIdentity(root, io = fs) {
  requireDirectory(root, "candidate directory identity root", io);
  const files = [];
  function visit(directory) {
    for (const entry of io.readdirSync(directory, { withFileTypes: true })) {
      const absolute = path.join(directory, entry.name);
      const stat = io.lstatSync(absolute);
      if (stat.isSymbolicLink()) throw new Error(`candidate directory contains a linked entry: ${absolute}`);
      if (stat.isDirectory()) visit(absolute);
      else if (stat.isFile()) {
        files.push({
          path: path.relative(root, absolute).replaceAll(path.sep, "/"),
          size: Number(stat.size),
          sha256: sha256File(absolute, io),
        });
      } else {
        throw new Error(`candidate directory contains a non-regular entry: ${absolute}`);
      }
    }
  }
  visit(root);
  files.sort((left, right) => left.path.localeCompare(right.path));
  if (files.length === 0) throw new Error("candidate directory identity cannot be empty");
  const canonical = files.map((file) => `${file.path}\0${file.size}\0${file.sha256}`).join("\n");
  return {
    files: files.length,
    total_size: files.reduce((total, file) => total + file.size, 0),
    content_set_sha256: crypto.createHash("sha256").update(canonical, "utf8").digest("hex"),
  };
}

function identitiesEqual(left, right) {
  return left?.files === right?.files
    && left?.total_size === right?.total_size
    && left?.content_set_sha256 === right?.content_set_sha256;
}

function assertDirectoryIdentity(root, expected, label, io = fs) {
  const observed = directoryIdentity(root, io);
  if (!identitiesEqual(observed, expected)) {
    throw new Error(`${label} content identity changed`);
  }
  return observed;
}

function beginDirectoryReplacement({ sourceRoot, targetRoot, stagingRoot, backupRoot, io = fs }) {
  const sourceStat = requireDirectory(sourceRoot, "replacement source", io);
  const targetStat = requireDirectory(targetRoot, "replacement target", io);
  const targetParentStat = requireDirectory(path.dirname(targetRoot), "replacement target parent", io);
  const stagingParentStat = requireDirectory(path.dirname(stagingRoot), "replacement staging parent", io);
  const backupParentStat = requireDirectory(path.dirname(backupRoot), "replacement backup parent", io);
  if (targetStat.dev !== targetParentStat.dev
      || targetParentStat.dev !== stagingParentStat.dev
      || targetParentStat.dev !== backupParentStat.dev) {
    throw new Error("candidate directory replacement requires one filesystem volume");
  }
  if (io.lstatSync(stagingRoot, { throwIfNoEntry: false })) {
    throw new Error("candidate directory replacement staging path already exists");
  }
  if (io.lstatSync(backupRoot, { throwIfNoEntry: false })) {
    throw new Error("candidate directory replacement backup path already exists");
  }
  const expected = directoryIdentity(sourceRoot, io);
  io.cpSync(sourceRoot, stagingRoot, {
    recursive: true,
    force: false,
    errorOnExist: true,
    verbatimSymlinks: true,
  });
  try {
    assertDirectoryIdentity(stagingRoot, expected, "staged candidate directory", io);
    io.renameSync(targetRoot, backupRoot);
    try {
      io.renameSync(stagingRoot, targetRoot);
      assertDirectoryIdentity(targetRoot, expected, "replacement candidate directory", io);
      return { backupRoot, expected, sourceRoot, stagingRoot, targetRoot };
    } catch (error) {
      if (!io.lstatSync(targetRoot, { throwIfNoEntry: false })
          && io.lstatSync(backupRoot, { throwIfNoEntry: false })) {
        io.renameSync(backupRoot, targetRoot);
      }
      throw error;
    }
  } finally {
    if (io.lstatSync(stagingRoot, { throwIfNoEntry: false })) {
      io.rmSync(stagingRoot, { recursive: true, force: true });
    }
  }
}

function commitDirectoryReplacement(transaction, io = fs) {
  assertDirectoryIdentity(transaction.targetRoot, transaction.expected, "committed candidate directory", io);
  requireDirectory(transaction.backupRoot, "replacement backup", io);
  io.rmSync(transaction.backupRoot, { recursive: true, force: false });
}

function rollbackDirectoryReplacement(transaction, io = fs) {
  requireDirectory(transaction.backupRoot, "replacement rollback backup", io);
  const target = io.lstatSync(transaction.targetRoot, { throwIfNoEntry: false });
  if (target && (!target.isDirectory() || target.isSymbolicLink())) {
    throw new Error("replacement rollback target is unsafe");
  }
  if (io.lstatSync(transaction.stagingRoot, { throwIfNoEntry: false })) {
    throw new Error("replacement rollback staging path already exists");
  }
  if (target) io.renameSync(transaction.targetRoot, transaction.stagingRoot);
  io.renameSync(transaction.backupRoot, transaction.targetRoot);
  if (io.lstatSync(transaction.stagingRoot, { throwIfNoEntry: false })) {
    io.rmSync(transaction.stagingRoot, { recursive: true, force: true });
  }
}

module.exports = {
  assertDirectoryIdentity,
  beginDirectoryReplacement,
  commitDirectoryReplacement,
  directoryIdentity,
  identitiesEqual,
  rollbackDirectoryReplacement,
};
