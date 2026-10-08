"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  TEMP_ROOT,
  assertOwnedRoot,
  createTemporaryRootTracker,
} = require("./support/temporary-root.cjs");

test("tracked temporary roots are removed by the registered per-test teardown", () => {
  let teardown = null;
  const temporaryRoot = createTemporaryRootTracker({
    afterEach: (callback) => { teardown = callback; },
  });
  const root = temporaryRoot("chriptmas-helper-test-");
  fs.mkdirSync(path.join(root, "nested"));
  fs.writeFileSync(path.join(root, "nested", "fixture.txt"), "fixture");

  assert.equal(path.dirname(root), TEMP_ROOT);
  assert.equal(fs.existsSync(root), true);
  teardown();
  assert.equal(fs.existsSync(root), false);
  assert.doesNotThrow(() => teardown());
});

test("temporary root tracker rejects broad or escaping prefixes and roots", () => {
  const temporaryRoot = createTemporaryRootTracker({ afterEach: () => {} });
  for (const prefix of ["tmp-", "../chriptmas-test-", "chriptmas/test-", "chriptmas-test"]) {
    assert.throws(() => temporaryRoot(prefix), /temporary_test_prefix_invalid/);
  }
  assert.throws(
    () => assertOwnedRoot(path.join(TEMP_ROOT, "..", "chriptmas-outside")),
    /temporary_test_root_invalid/,
  );
  temporaryRoot.cleanup();
});
