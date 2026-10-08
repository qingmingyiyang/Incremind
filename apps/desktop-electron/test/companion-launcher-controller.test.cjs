const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const { CompanionLauncherController, normalizeBookmark } = require("../src/companion/launcher-controller.cjs");
const { createTemporaryRootTracker } = require("./support/temporary-root.cjs");

const temporaryRoot = createTemporaryRootTracker(test);

function fixture(overrides = {}) {
  const root = temporaryRoot("chriptmas-launcher-");
  const executable = path.join(root, "Program With Spaces.exe");
  fs.writeFileSync(executable, "test executable");
  const calls = [];
  const controller = new CompanionLauncherController({
    statePath: path.join(root, "launchers.json"),
    openExternal: async (url) => calls.push(["url", url]),
    spawnProcess: (file, args, options) => {
      calls.push(["program", file, args, options]);
      return { unref: () => calls.push(["unref"]) };
    },
    idFactory: (() => {
      const ids = ["00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"];
      return () => ids.shift();
    })(),
    ...overrides,
  });
  return { root, executable, calls, controller };
}

test("registers a selected executable and launches without shell or arguments", async () => {
  const value = fixture();
  const entry = value.controller.addProgram({ name: "  我的 IDE  ", selectedPath: value.executable });
  assert.deepEqual(entry, { id: "program:00000000-0000-4000-8000-000000000001", kind: "program", name: "我的 IDE", target: "Program With Spaces.exe" });
  const result = await value.controller.launch({ id: entry.id });
  assert.deepEqual(result, { status: "launched", id: entry.id, kind: "program", name: "我的 IDE" });
  assert.equal(value.calls[0][0], "program");
  assert.deepEqual(value.calls[0][2], []);
  assert.equal(value.calls[0][3].shell, false);
  assert.equal(value.calls[0][3].detached, true);
});

test("accepts a main-owned bounded launch argument resolver without changing ordinary launches", async () => {
  const value = fixture({ launchArgs: () => ["--user-data-dir=C:\\isolated"] });
  const entry = value.controller.addProgram({ name: "E2E", selectedPath: value.executable });
  await value.controller.launch({ id: entry.id });
  assert.deepEqual(value.calls[0][2], ["--user-data-dir=C:\\isolated"]);
  const invalid = fixture({ launchArgs: () => ["--one", "--two"] });
  const invalidEntry = invalid.controller.addProgram({ name: "bad", selectedPath: invalid.executable });
  await assert.rejects(invalid.controller.launch({ id: invalidEntry.id }), /arguments_invalid/);
});

test("ensure program reuses only an exact current authority", () => {
  const value = fixture();
  const first = value.controller.ensureProgram({ name: "E2E target", selectedPath: value.executable });
  const second = value.controller.ensureProgram({ name: "Renamed E2E target", selectedPath: value.executable });
  assert.equal(first.id, second.id);
  assert.equal(second.name, "E2E target");
  assert.equal(value.controller.list().entries.length, 1);
  const sameNameDifferentAuthority = path.join(value.root, "nested", path.basename(value.executable));
  fs.mkdirSync(path.dirname(sameNameDifferentAuthority));
  fs.writeFileSync(sameNameDifferentAuthority, "different executable");
  const different = value.controller.ensureProgram({ name: "Other target", selectedPath: sameNameDifferentAuthority });
  assert.notEqual(different.id, first.id);
});

test("rejects missing, replaced, relative and unsupported program authorities", async () => {
  const value = fixture();
  const entry = value.controller.addProgram({ name: "IDE", selectedPath: value.executable });
  fs.appendFileSync(value.executable, "changed");
  await assert.rejects(value.controller.launch({ id: entry.id }), /authority_changed/);
  assert.throws(() => value.controller.addProgram({ name: "bad", selectedPath: "relative.exe" }), /program_invalid/);
  const script = path.join(value.root, "bad.cmd");
  fs.writeFileSync(script, "echo bad");
  assert.throws(() => value.controller.addProgram({ name: "bad", selectedPath: script }), /program_invalid/);
});

test("normalizes and opens only credential-free fragment-free HTTPS bookmarks", async () => {
  const value = fixture();
  const entry = value.controller.addBookmark({ name: "文档", url: "https://EXAMPLE.com/docs?q=1" });
  assert.deepEqual(entry, { id: "bookmark:00000000-0000-4000-8000-000000000001", kind: "bookmark", name: "文档", target: "example.com" });
  await value.controller.launch({ id: entry.id });
  assert.deepEqual(value.calls, [["url", "https://example.com/docs?q=1"]]);
  for (const url of ["http://example.com", "file:///tmp/a", "javascript:alert(1)", "data:text/plain,x", "https://user:pass@example.com", "https://example.com/#secret", "https://example.com/#", " https://example.com/"]) {
    assert.throws(() => normalizeBookmark(url), /bookmark_invalid/);
  }
});

test("persists stable IDs across rename, restart and delete without projecting secrets", () => {
  const value = fixture();
  const entry = value.controller.addBookmark({ name: "入口", url: "https://example.com/path?token=secret" });
  assert.equal(JSON.stringify(value.controller.list()).includes("token=secret"), false);
  assert.equal(value.controller.rename({ id: entry.id, name: "新入口" }).id, entry.id);
  const restarted = new CompanionLauncherController({
    statePath: path.join(value.root, "launchers.json"),
    openExternal: async () => {},
  });
  assert.equal(restarted.list().entries[0].name, "新入口");
  assert.deepEqual(restarted.remove({ id: entry.id }), { status: "deleted", id: entry.id });
  assert.deepEqual(restarted.list().entries, []);
});

test("corrupt and duplicate state fails closed and is not overwritten", () => {
  const value = fixture();
  const statePath = path.join(value.root, "launchers.json");
  fs.writeFileSync(statePath, JSON.stringify({ version: 1, entries: [
    { id: "bookmark:00000000-0000-4000-8000-000000000001", kind: "bookmark", name: "A", url: "https://example.com/" },
    { id: "bookmark:00000000-0000-4000-8000-000000000001", kind: "bookmark", name: "B", url: "https://example.org/" },
  ] }));
  const controller = new CompanionLauncherController({ statePath, openExternal: async () => {} });
  assert.deepEqual(controller.list(), { state: "invalid", entries: [] });
  assert.throws(() => controller.addBookmark({ name: "C", url: "https://example.net" }), /state_invalid/);
  assert.equal(JSON.parse(fs.readFileSync(statePath, "utf8")).entries.length, 2);
});

test("unknown IDs and process or browser failures stay explicit", async () => {
  const value = fixture({ openExternal: async () => { throw new Error("browser failed"); } });
  assert.throws(() => value.controller.remove({ id: "bookmark:00000000-0000-4000-8000-000000000099" }), /unknown/);
  const entry = value.controller.addBookmark({ name: "A", url: "https://example.com" });
  await assert.rejects(value.controller.launch({ id: entry.id }), /browser failed/);
  const failedProgram = fixture({ spawnProcess: () => { throw new Error("process failed"); } });
  const program = failedProgram.controller.addProgram({ name: "Program", selectedPath: failedProgram.executable });
  await assert.rejects(failedProgram.controller.launch({ id: program.id }), /process failed/);
});

test("rejects generated duplicate IDs instead of persisting an invalid state", () => {
  const idFactory = () => "00000000-0000-4000-8000-000000000001";
  const value = fixture({ idFactory });
  value.controller.addBookmark({ name: "A", url: "https://example.com" });
  assert.throws(() => value.controller.addBookmark({ name: "B", url: "https://example.org" }), /id_invalid/);
  assert.equal(value.controller.list().entries.length, 1);
});
