const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { MAX_MANUAL_BYTES, resolveCompanionManualPath } = require("../src/companion/manual-path.cjs");

function workspace() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-manual-"));
  const repository = path.join(root, "repository");
  const resources = path.join(root, "resources");
  const userData = path.join(root, "user-data");
  fs.mkdirSync(path.join(resources, "manual"), { recursive: true });
  fs.mkdirSync(repository);
  fs.mkdirSync(userData);
  return { root, repository, resources, userData };
}

test("development manual resolves the repository authority", (t) => {
  const paths = workspace();
  t.after(() => fs.rmSync(paths.root, { recursive: true, force: true }));
  fs.writeFileSync(path.join(paths.repository, "readme.md"), "# dev", "utf8");
  assert.equal(resolveCompanionManualPath({ packaged: false, repositoryRoot: paths.repository, resourcesRoot: paths.resources, userDataRoot: paths.userData }), path.join(paths.repository, "readme.md"));
});

test("packaged manual seeds once and preserves the editable user copy", (t) => {
  const paths = workspace();
  t.after(() => fs.rmSync(paths.root, { recursive: true, force: true }));
  fs.writeFileSync(path.join(paths.resources, "manual", "readme.md"), "seed-one", "utf8");
  const options = { packaged: true, repositoryRoot: paths.repository, resourcesRoot: paths.resources, userDataRoot: paths.userData };
  const target = resolveCompanionManualPath(options);
  fs.writeFileSync(target, "user-edit", "utf8");
  fs.writeFileSync(path.join(paths.resources, "manual", "readme.md"), "seed-two", "utf8");
  assert.equal(resolveCompanionManualPath(options), target);
  assert.equal(fs.readFileSync(target, "utf8"), "user-edit");
});

test("manual resolver rejects missing and oversized authorities", (t) => {
  const paths = workspace();
  t.after(() => fs.rmSync(paths.root, { recursive: true, force: true }));
  assert.throws(() => resolveCompanionManualPath({ packaged: false, repositoryRoot: paths.repository, resourcesRoot: paths.resources, userDataRoot: paths.userData }));
  fs.writeFileSync(path.join(paths.repository, "readme.md"), Buffer.alloc(MAX_MANUAL_BYTES + 1));
  assert.throws(() => resolveCompanionManualPath({ packaged: false, repositoryRoot: paths.repository, resourcesRoot: paths.resources, userDataRoot: paths.userData }), /file_invalid/);
});
