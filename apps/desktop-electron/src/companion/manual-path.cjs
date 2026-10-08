const fs = require("node:fs");
const path = require("node:path");

const MAX_MANUAL_BYTES = 2 * 1024 * 1024;

function resolveCompanionManualPath({ packaged, repositoryRoot, resourcesRoot, userDataRoot }) {
  const repository = requireAbsoluteDirectory(repositoryRoot, "repository");
  const userData = requireAbsoluteDirectory(userDataRoot, "user_data", { create: true });
  if (!packaged) return requireSafeManual(path.join(repository, "readme.md"), repository);
  const resources = requireAbsoluteDirectory(resourcesRoot, "resources");
  const seed = requireSafeManual(path.join(resources, "manual", "readme.md"), resources);
  const targetRoot = path.join(userData, "companion");
  const target = path.join(targetRoot, "readme.md");
  fs.mkdirSync(targetRoot, { recursive: true });
  if (!fs.existsSync(target)) {
    const temporary = path.join(targetRoot, `.readme.${process.pid}.${Date.now()}.tmp`);
    fs.copyFileSync(seed, temporary, fs.constants.COPYFILE_EXCL);
    try { fs.renameSync(temporary, target); } finally { if (fs.existsSync(temporary)) fs.unlinkSync(temporary); }
  }
  return requireSafeManual(target, userData);
}

function requireAbsoluteDirectory(value, label, { create = false } = {}) {
  if (typeof value !== "string" || !path.isAbsolute(value)) throw new Error(`companion_manual_${label}_invalid`);
  const resolved = path.resolve(value);
  if (create) fs.mkdirSync(resolved, { recursive: true });
  const stat = fs.lstatSync(resolved);
  if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error(`companion_manual_${label}_invalid`);
  return resolved;
}

function requireSafeManual(target, allowedRoot) {
  const resolved = path.resolve(target);
  const root = path.resolve(allowedRoot);
  const relative = path.relative(root, resolved);
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) throw new Error("companion_manual_path_invalid");
  let current = path.dirname(resolved);
  while (current !== root) {
    const parentStat = fs.lstatSync(current);
    if (!parentStat.isDirectory() || parentStat.isSymbolicLink()) throw new Error("companion_manual_path_invalid");
    const parent = path.dirname(current);
    if (parent === current) throw new Error("companion_manual_path_invalid");
    current = parent;
  }
  const stat = fs.lstatSync(resolved);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.size > MAX_MANUAL_BYTES) throw new Error("companion_manual_file_invalid");
  return resolved;
}

module.exports = { MAX_MANUAL_BYTES, resolveCompanionManualPath };
