"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const TEMP_ROOT = path.resolve(os.tmpdir());
const PREFIX = /^chriptmas-[a-z0-9-]+-$/i;

function assertOwnedRoot(root) {
  const resolved = path.resolve(root);
  if (path.dirname(resolved) !== TEMP_ROOT || !path.basename(resolved).startsWith("chriptmas-")) {
    throw new Error("temporary_test_root_invalid");
  }
  return resolved;
}

function createTemporaryRootTracker(testApi, io = fs) {
  if (!testApi || typeof testApi.afterEach !== "function") {
    throw new TypeError("temporary_test_api_invalid");
  }
  const roots = new Set();
  const cleanup = () => {
    const errors = [];
    for (const root of roots) {
      try {
        const owned = assertOwnedRoot(root);
        io.rmSync(owned, {
          recursive: true,
          force: true,
          maxRetries: 3,
          retryDelay: 50,
        });
        roots.delete(root);
      } catch (error) {
        errors.push(error);
      }
    }
    if (errors.length > 0) {
      throw new AggregateError(errors, "temporary_test_cleanup_failed");
    }
  };
  testApi.afterEach(cleanup);
  const create = (prefix) => {
    if (typeof prefix !== "string" || !PREFIX.test(prefix)) {
      throw new Error("temporary_test_prefix_invalid");
    }
    const root = assertOwnedRoot(io.mkdtempSync(path.join(TEMP_ROOT, prefix)));
    roots.add(root);
    return root;
  };
  create.cleanup = cleanup;
  return create;
}

module.exports = {
  TEMP_ROOT,
  assertOwnedRoot,
  createTemporaryRootTracker,
};
