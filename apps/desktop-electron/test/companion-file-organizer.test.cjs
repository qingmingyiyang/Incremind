const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { CompanionFileOrganizer, CompanionFileOrganizerError, categoryFor } = require("../src/companion/file-organizer.cjs");
const { createTemporaryRootTracker } = require("./support/temporary-root.cjs");

const temporaryRoot = createTemporaryRootTracker(test);

function harness() {
  const root = temporaryRoot("chriptmas-organizer-");
  const source = path.join(root, "source");
  const target = path.join(root, "target");
  fs.mkdirSync(source); fs.mkdirSync(target);
  let now = 1000;
  const organizer = new CompanionFileOrganizer({ journalPath: path.join(root, "state", "journal.json"), now: () => now, randomId: () => "operation-1" });
  return { root, source, target, organizer, advance: (value) => { now += value; } };
}

test("classifies extensions with a closed local-only taxonomy", () => {
  assert.equal(categoryFor("PHOTO.JPEG"), "images");
  assert.equal(categoryFor("notes.md"), "documents");
  assert.equal(categoryFor("archive.7z"), "archives");
  assert.equal(categoryFor("unknown.xyz"), "other");
});

test("previews first-level regular files then moves and safely undoes them", () => {
  const h = harness();
  fs.writeFileSync(path.join(h.source, "photo.png"), "image");
  fs.writeFileSync(path.join(h.source, "notes.txt"), "notes");
  fs.mkdirSync(path.join(h.source, "nested"));

  const preview = h.organizer.preview(h.source, h.target);
  assert.deepEqual(preview.categories, { images: 1, documents: 1 });
  assert.equal(preview.file_count, 2); assert.equal(preview.skipped, 1);
  assert.equal(JSON.stringify(preview).includes(h.source), false);

  const result = h.organizer.execute(preview.plan_id);
  assert.equal(result.status, "completed"); assert.equal(result.moved, 2);
  assert.equal(fs.readFileSync(path.join(h.target, "图片", "photo.png"), "utf8"), "image");
  assert.throws(() => h.organizer.execute(preview.plan_id), /organizer_plan_expired/);

  const undone = h.organizer.undo(result.operation_id);
  assert.deepEqual(undone, { operation_id: "operation-1", status: "undone", restored: 2, skipped: 0 });
  assert.equal(fs.readFileSync(path.join(h.source, "notes.txt"), "utf8"), "notes");
});

test("destination conflicts stop without overwrite and retain a partial journal", () => {
  const h = harness();
  fs.writeFileSync(path.join(h.source, "a.png"), "a");
  fs.writeFileSync(path.join(h.source, "b.txt"), "b");
  fs.mkdirSync(path.join(h.target, "文档"));
  fs.writeFileSync(path.join(h.target, "文档", "b.txt"), "existing");

  const result = h.organizer.execute(h.organizer.preview(h.source, h.target).plan_id);

  assert.equal(result.status, "partial");
  assert.equal(result.moved, 1);
  assert.equal(result.error, "organizer_destination_exists");
  assert.equal(fs.readFileSync(path.join(h.target, "文档", "b.txt"), "utf8"), "existing");
});

test("changed files and expired plans fail closed", () => {
  const h = harness();
  const file = path.join(h.source, "notes.txt");
  fs.writeFileSync(file, "before");
  const changed = h.organizer.preview(h.source, h.target);
  fs.writeFileSync(file, "after-change");
  assert.equal(h.organizer.execute(changed.plan_id).error, "organizer_file_changed");

  const expired = h.organizer.preview(h.source, h.target);
  h.advance(5 * 60_000 + 1);
  assert.throws(() => h.organizer.execute(expired.plan_id), /organizer_plan_expired/);
});

test("undo never overwrites a later source file or restores a changed target", () => {
  const h = harness();
  fs.writeFileSync(path.join(h.source, "notes.txt"), "original");
  const result = h.organizer.execute(h.organizer.preview(h.source, h.target).plan_id);
  fs.writeFileSync(path.join(h.source, "notes.txt"), "later");

  const undone = h.organizer.undo(result.operation_id);

  assert.equal(undone.status, "undo_partial"); assert.equal(undone.skipped, 1);
  assert.equal(fs.readFileSync(path.join(h.source, "notes.txt"), "utf8"), "later");
});

test("rejects same nested cross-volume and symlink roots", (t) => {
  const h = harness();
  assert.throws(() => h.organizer.preview(h.source, h.source), /organizer_roots_invalid/);
  const nested = path.join(h.source, "nested"); fs.mkdirSync(nested);
  assert.throws(() => h.organizer.preview(h.source, nested), /organizer_roots_invalid/);
  const link = path.join(h.root, "link");
  try { fs.symlinkSync(h.source, link, "junction"); } catch { t.skip("junction creation unavailable"); return; }
  assert.throws(() => h.organizer.preview(link, h.target), CompanionFileOrganizerError);
});

test("corrupt journal fails closed and survives a fresh controller", () => {
  const h = harness();
  fs.writeFileSync(path.join(h.source, "notes.txt"), "notes");
  const result = h.organizer.execute(h.organizer.preview(h.source, h.target).plan_id);
  const restarted = new CompanionFileOrganizer({ journalPath: path.join(h.root, "state", "journal.json") });
  assert.equal(restarted.history()[0].operation_id, result.operation_id);
  fs.writeFileSync(path.join(h.root, "state", "journal.json"), "not-json");
  assert.throws(() => restarted.history(), /organizer_journal_invalid/);
});
