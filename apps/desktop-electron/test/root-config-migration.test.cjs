const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  DATA_ROOT_POINTER_FILE,
  DATA_ROOT_PREVIOUS_POINTER_FILE,
  ROOT_MIGRATION_JOURNAL_FILE,
  ROOT_MIGRATION_PREVIOUS_JOURNAL_FILE,
  RootConfigMigrationController,
  diagnoseRootConfig,
  resolvePackagedVaultRoot,
  resolveRootConfig,
} = require("../src/vault-root.cjs");

function temporaryRoot() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-root-config-"));
}

function writePointer(userDataDir, config) {
  fs.mkdirSync(userDataDir, { recursive: true });
  fs.writeFileSync(path.join(userDataDir, DATA_ROOT_POINTER_FILE), JSON.stringify(config));
}

function sourceRoots(root) {
  const source = { vaultRoot: path.join(root, "source-vault"), modelRoot: path.join(root, "source-model"), mediaRoot: path.join(root, "source-media") };
  for (const [role, candidate] of Object.entries(source)) {
    fs.mkdirSync(candidate, { recursive: true });
    fs.writeFileSync(path.join(candidate, `${role}.txt`), role);
  }
  return source;
}

function targetRoots(root) {
  return { vaultRoot: path.join(root, "target-vault"), modelRoot: path.join(root, "target-model"), mediaRoot: path.join(root, "target-media") };
}

test("versioned pointers expose separate Vault Model Media roots while old pointer callers remain compatible", () => {
  const root = temporaryRoot();
  const userDataDir = path.join(root, "user-data");
  const config = targetRoots(root);
  writePointer(userDataDir, { version: 1, root: config.vaultRoot, roots: { model: config.modelRoot, media: config.mediaRoot } });

  assert.deepEqual(resolveRootConfig({ userDataDir }).config, { version: 1, ...config, source: "versioned_pointer" });
  assert.deepEqual(resolvePackagedVaultRoot({ userDataDir }), { mode: "configured", workingDir: config.vaultRoot, pointerPath: path.join(userDataDir, DATA_ROOT_POINTER_FILE) });

  writePointer(userDataDir, { root: config.vaultRoot });
  assert.deepEqual(resolveRootConfig({ userDataDir }).config, { version: 0, vaultRoot: config.vaultRoot, modelRoot: config.vaultRoot, mediaRoot: config.vaultRoot, source: "legacy_pointer" });
});

test("diagnostics distinguish configured role paths from their current probes", () => {
  const root = temporaryRoot();
  const userDataDir = path.join(root, "user-data");
  const config = targetRoots(root);
  writePointer(userDataDir, { version: 1, root: config.vaultRoot, roots: { model: config.modelRoot, media: config.mediaRoot } });
  const result = diagnoseRootConfig({ userDataDir, probePath: (candidate, role) => ({ reachable: role !== "media", observed: `${candidate}-actual` }) });

  assert.equal(result.configured.roots.model, config.modelRoot);
  assert.deepEqual(result.actual.media, { path: config.mediaRoot, reachable: false, observed: `${config.mediaRoot}-actual` });
});

test("preflight rejects unavailable unwritable and insufficient-space targets before writing a journal or pointer", () => {
  for (const [name, options, error] of [
    ["unavailable", { targetAvailable: () => false }, /target_unavailable/],
    ["unwritable", { targetWritable: () => false }, /target_unwritable/],
    ["space", { estimateBytes: () => 2, availableBytes: () => 1 }, /insufficient_space/],
  ]) {
    const root = temporaryRoot();
    const userDataDir = path.join(root, "user-data");
    const source = sourceRoots(root);
    writePointer(userDataDir, { version: 1, root: source.vaultRoot, roots: { model: source.modelRoot, media: source.mediaRoot } });
    const controller = new RootConfigMigrationController({ userDataDir, ...options });
    assert.throws(() => controller.migrate({ target: targetRoots(root) }), error, name);
    assert.equal(fs.existsSync(path.join(userDataDir, ROOT_MIGRATION_JOURNAL_FILE)), false, name);
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(userDataDir, DATA_ROOT_POINTER_FILE))), { version: 1, root: source.vaultRoot, roots: { model: source.modelRoot, media: source.mediaRoot } }, name);
  }
});

test("copy interruption leaves the old pointer and durable journal, then restart uses a fresh staging attempt and switches only after all role copies", () => {
  const root = temporaryRoot();
  const userDataDir = path.join(root, "user-data");
  const source = sourceRoots(root);
  const target = targetRoots(root);
  writePointer(userDataDir, { version: 1, root: source.vaultRoot, roots: { model: source.modelRoot, media: source.mediaRoot } });
  let copies = 0;
  const interrupted = new RootConfigMigrationController({
    userDataDir,
    copyTree: (from, staging) => { fs.cpSync(from, staging, { recursive: true }); copies += 1; if (copies === 1) throw new Error("copy_interrupted"); },
  });
  assert.throws(() => interrupted.migrate({ target }), /copy_interrupted/);
  assert.equal(JSON.parse(fs.readFileSync(path.join(userDataDir, DATA_ROOT_POINTER_FILE))).root, source.vaultRoot);
  const interruptedJournal = JSON.parse(fs.readFileSync(path.join(userDataDir, ROOT_MIGRATION_JOURNAL_FILE)));
  assert.equal(interruptedJournal.status, "copying");
  assert.equal(interruptedJournal.operations[0].status, "copying");

  const restarted = new RootConfigMigrationController({ userDataDir });
  const result = restarted.recoverPending();
  assert.equal(result.status, "switched");
  assert.deepEqual(resolveRootConfig({ userDataDir }).config, { version: 1, ...target, source: "versioned_pointer" });
  assert.equal(fs.readFileSync(path.join(target.vaultRoot, "vaultRoot.txt"), "utf8"), "vaultRoot");
  assert.equal(fs.readFileSync(path.join(target.modelRoot, "modelRoot.txt"), "utf8"), "modelRoot");
  assert.equal(fs.readFileSync(path.join(target.mediaRoot, "mediaRoot.txt"), "utf8"), "mediaRoot");
  assert.equal(JSON.parse(fs.readFileSync(path.join(userDataDir, ROOT_MIGRATION_JOURNAL_FILE))).status, "switched");
});

test("recovery preserves role copies completed before a later role is interrupted", () => {
  const root = temporaryRoot();
  const userDataDir = path.join(root, "user-data");
  const source = sourceRoots(root);
  const target = targetRoots(root);
  writePointer(userDataDir, { version: 1, root: source.vaultRoot, roots: { model: source.modelRoot, media: source.mediaRoot } });
  let copies = 0;
  const interrupted = new RootConfigMigrationController({
    userDataDir,
    copyTree: (from, staging) => {
      copies += 1;
      if (copies === 2) throw new Error("later_copy_interrupted");
      fs.cpSync(from, staging, { recursive: true });
    },
  });

  assert.throws(() => interrupted.migrate({ target }), /later_copy_interrupted/);
  assert.equal(fs.existsSync(target.vaultRoot), true);
  assert.equal(JSON.parse(fs.readFileSync(path.join(userDataDir, ROOT_MIGRATION_JOURNAL_FILE))).operations[0].status, "copied");

  const result = new RootConfigMigrationController({ userDataDir }).recoverPending();
  assert.equal(result.status, "switched");
  assert.equal(fs.readFileSync(path.join(target.vaultRoot, "vaultRoot.txt"), "utf8"), "vaultRoot");
  assert.equal(fs.readFileSync(path.join(target.modelRoot, "modelRoot.txt"), "utf8"), "modelRoot");
  assert.equal(fs.readFileSync(path.join(target.mediaRoot, "mediaRoot.txt"), "utf8"), "mediaRoot");
});

test("recovery is restart-safe after a completed pointer switch and never copies again", () => {
  const root = temporaryRoot();
  const userDataDir = path.join(root, "user-data");
  const source = sourceRoots(root);
  const target = targetRoots(root);
  writePointer(userDataDir, { version: 1, root: source.vaultRoot, roots: { model: source.modelRoot, media: source.mediaRoot } });
  const controller = new RootConfigMigrationController({ userDataDir });
  assert.equal(controller.migrate({ target }).status, "switched");
  const restarted = new RootConfigMigrationController({ userDataDir, copyTree: () => { throw new Error("must_not_copy"); } });
  assert.equal(restarted.recoverPending().status, "switched");
});

test("recovery recognizes a receipt when the target rename succeeds before its copied checkpoint", () => {
  const root = temporaryRoot();
  const userDataDir = path.join(root, "user-data");
  const source = sourceRoots(root);
  const target = targetRoots(root);
  writePointer(userDataDir, { version: 1, root: source.vaultRoot, roots: { model: source.modelRoot, media: source.mediaRoot } });
  let interrupted = false;
  const controller = new RootConfigMigrationController({
    userDataDir,
    rename: (from, to) => {
      fs.renameSync(from, to);
      if (!interrupted && to === target.vaultRoot) { interrupted = true; throw new Error("crash_after_target_rename"); }
    },
  });

  assert.throws(() => controller.migrate({ target }), /crash_after_target_rename/);
  assert.equal(fs.existsSync(target.vaultRoot), true);
  assert.equal(JSON.parse(fs.readFileSync(path.join(userDataDir, ROOT_MIGRATION_JOURNAL_FILE))).operations[0].status, "copying");

  assert.equal(new RootConfigMigrationController({ userDataDir }).recoverPending().status, "switched");
  assert.equal(fs.readFileSync(path.join(target.vaultRoot, "vaultRoot.txt"), "utf8"), "vaultRoot");
  assert.equal(fs.existsSync(path.join(target.vaultRoot, ".chriptmas-root-migration-receipt.json")), false);
});

test("recovery refuses a receipt-bearing target whose published content no longer matches the frozen manifest", () => {
  const root = temporaryRoot();
  const userDataDir = path.join(root, "user-data");
  const source = sourceRoots(root);
  const target = targetRoots(root);
  writePointer(userDataDir, { version: 1, root: source.vaultRoot, roots: { model: source.modelRoot, media: source.mediaRoot } });
  let interrupted = false;
  const controller = new RootConfigMigrationController({
    userDataDir,
    rename: (from, to) => {
      fs.renameSync(from, to);
      if (!interrupted && to === target.vaultRoot) { interrupted = true; throw new Error("crash_after_target_rename"); }
    },
  });

  assert.throws(() => controller.execute({ target }), /crash_after_target_rename/);
  fs.appendFileSync(path.join(target.vaultRoot, "vaultRoot.txt"), "external-change");
  assert.throws(() => new RootConfigMigrationController({ userDataDir }).recover(), /target_verification_failed/);
  assert.equal(resolveRootConfig({ userDataDir }).config.vaultRoot, source.vaultRoot);
});

test("preflight rejects overlapping roots and unmanaged SQLite copies before creating a journal", () => {
  for (const [name, target, arrange, error] of [
    ["nested targets", (root) => ({ vaultRoot: path.join(root, "target-vault"), modelRoot: path.join(root, "target-vault", "models"), mediaRoot: path.join(root, "target-media") }), () => {}, /target_roots_overlap/],
    ["target overlaps another source", (root, source) => ({ vaultRoot: path.join(root, "target-vault"), modelRoot: source.vaultRoot, mediaRoot: path.join(root, "target-media") }), () => {}, /source_target_overlap/],
    ["sqlite requires an explicit consistency boundary", (root) => targetRoots(root), (source) => fs.writeFileSync(path.join(source.vaultRoot, "app.db"), "synthetic"), /database_snapshot_required/],
  ]) {
    const root = temporaryRoot();
    const userDataDir = path.join(root, "user-data");
    const source = sourceRoots(root);
    arrange(source);
    writePointer(userDataDir, { version: 1, root: source.vaultRoot, roots: { model: source.modelRoot, media: source.mediaRoot } });
    assert.throws(() => new RootConfigMigrationController({ userDataDir }).migrate({ target: target(root, source) }), error, name);
    assert.equal(fs.existsSync(path.join(userDataDir, ROOT_MIGRATION_JOURNAL_FILE)), false, name);
  }
});

test("preflight rejects nested source and target roots in either role order", () => {
  for (const [name, sourceFactory, targetFactory, error] of [
    [
      "source child before parent",
      (root) => ({ vaultRoot: path.join(root, "source-parent", "vault"), modelRoot: path.join(root, "source-parent"), mediaRoot: path.join(root, "source-media") }),
      (root) => targetRoots(root),
      /source_roots_overlap/,
    ],
    [
      "target child before parent",
      (root) => sourceRoots(root),
      (root) => ({ vaultRoot: path.join(root, "target-parent", "vault"), modelRoot: path.join(root, "target-parent"), mediaRoot: path.join(root, "target-media") }),
      /target_roots_overlap/,
    ],
  ]) {
    const root = temporaryRoot();
    const userDataDir = path.join(root, "user-data");
    const source = sourceFactory(root);
    for (const candidate of Object.values(source)) fs.mkdirSync(candidate, { recursive: true });
    writePointer(userDataDir, { version: 1, root: source.vaultRoot, roots: { model: source.modelRoot, media: source.mediaRoot } });
    assert.throws(() => new RootConfigMigrationController({ userDataDir }).preflight({ target: targetFactory(root) }), error, name);
  }
});

test("preflight treats Windows path case variants as the same root", { skip: process.platform !== "win32" }, () => {
  const root = temporaryRoot();
  const userDataDir = path.join(root, "user-data");
  const source = sourceRoots(root);
  writePointer(userDataDir, { version: 1, root: source.vaultRoot, roots: { model: source.modelRoot, media: source.mediaRoot } });
  const target = targetRoots(root);
  target.modelRoot = source.vaultRoot.toUpperCase();
  assert.throws(() => new RootConfigMigrationController({ userDataDir }).preflight({ target }), /source_target_overlap/);
});

test("a legacy root that contains AppData migration control state fails closed", () => {
  const root = temporaryRoot();
  const userDataDir = path.join(root, "legacy-user-data");
  fs.mkdirSync(path.join(userDataDir, "library"), { recursive: true });
  assert.throws(() => new RootConfigMigrationController({ userDataDir }).preflight({ target: targetRoots(root) }), /control_path_overlap/);
  assert.equal(fs.existsSync(path.join(userDataDir, ROOT_MIGRATION_JOURNAL_FILE)), false);
});

test("a SQLite source requires both an explicit quiescence boundary and a copied-database verifier", () => {
  const root = temporaryRoot();
  const userDataDir = path.join(root, "user-data");
  const source = sourceRoots(root);
  fs.writeFileSync(path.join(source.vaultRoot, "app.db"), "synthetic");
  writePointer(userDataDir, { version: 1, root: source.vaultRoot, roots: { model: source.modelRoot, media: source.mediaRoot } });

  assert.throws(() => new RootConfigMigrationController({ userDataDir, assertSourceQuiescent: () => {} }).preflight({ target: targetRoots(root) }), /database_verifier_required/);
  assert.equal(fs.existsSync(path.join(userDataDir, ROOT_MIGRATION_JOURNAL_FILE)), false);
});

test("preflight aggregates the required staging capacity on one target volume", () => {
  const root = temporaryRoot();
  const userDataDir = path.join(root, "user-data");
  const source = sourceRoots(root);
  writePointer(userDataDir, { version: 1, root: source.vaultRoot, roots: { model: source.modelRoot, media: source.mediaRoot } });

  assert.throws(() => new RootConfigMigrationController({ userDataDir, estimateBytes: () => 5, availableBytes: () => 10 }).migrate({ target: targetRoots(root) }), /insufficient_space/);
  assert.equal(fs.existsSync(path.join(userDataDir, ROOT_MIGRATION_JOURNAL_FILE)), false);
});

test("journals and pointers replace through sibling temporary files while preserving the old pointer", () => {
  const root = temporaryRoot();
  const userDataDir = path.join(root, "user-data");
  const source = sourceRoots(root);
  const target = targetRoots(root);
  const priorPointer = { version: 1, root: source.vaultRoot, roots: { model: source.modelRoot, media: source.mediaRoot } };
  writePointer(userDataDir, priorPointer);
  const replacements = [];
  const result = new RootConfigMigrationController({
    userDataDir,
    rename: (from, to) => { replacements.push({ from, to }); fs.renameSync(from, to); },
  }).migrate({ target });

  assert.equal(result.status, "switched");
  for (const destination of [path.join(userDataDir, ROOT_MIGRATION_JOURNAL_FILE), path.join(userDataDir, DATA_ROOT_POINTER_FILE), path.join(userDataDir, DATA_ROOT_PREVIOUS_POINTER_FILE)]) {
    const replacement = replacements.find((entry) => entry.to === destination);
    assert.ok(replacement, destination);
    assert.match(replacement.from, /\.tmp-/);
  }
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(userDataDir, DATA_ROOT_PREVIOUS_POINTER_FILE), "utf8")), priorPointer);
});

test("a second migration must recover an unfinished journal instead of overwriting it", () => {
  const root = temporaryRoot();
  const userDataDir = path.join(root, "user-data");
  const source = sourceRoots(root);
  const target = targetRoots(root);
  writePointer(userDataDir, { version: 1, root: source.vaultRoot, roots: { model: source.modelRoot, media: source.mediaRoot } });
  assert.throws(() => new RootConfigMigrationController({ userDataDir, copyTree: () => { throw new Error("interrupted"); } }).migrate({ target }), /interrupted/);
  assert.throws(() => new RootConfigMigrationController({ userDataDir }).migrate({ target: targetRoots(path.join(root, "other")) }), /recovery_required/);
});

test("an interrupted previous journal blocks a new migration even when its primary journal is missing", () => {
  const root = temporaryRoot();
  const userDataDir = path.join(root, "user-data");
  const source = sourceRoots(root);
  const target = targetRoots(root);
  writePointer(userDataDir, { version: 1, root: source.vaultRoot, roots: { model: source.modelRoot, media: source.mediaRoot } });
  assert.throws(() => new RootConfigMigrationController({ userDataDir, copyTree: () => { throw new Error("interrupted"); } }).migrate({ target }), /interrupted/);
  fs.unlinkSync(path.join(userDataDir, ROOT_MIGRATION_JOURNAL_FILE));
  assert.throws(() => new RootConfigMigrationController({ userDataDir }).migrate({ target: targetRoots(path.join(root, "other")) }), /recovery_required/);
});

test("corrupt current journal falls back to the last complete journal and uses a fresh staging attempt", () => {
  const root = temporaryRoot();
  const userDataDir = path.join(root, "user-data");
  const source = sourceRoots(root);
  const target = targetRoots(root);
  writePointer(userDataDir, { version: 1, root: source.vaultRoot, roots: { model: source.modelRoot, media: source.mediaRoot } });
  const interrupted = new RootConfigMigrationController({
    userDataDir,
    copyTree: (from, staging) => { fs.cpSync(from, staging, { recursive: true }); throw new Error("interrupted"); },
  });
  assert.throws(() => interrupted.migrate({ target }), /interrupted/);
  assert.equal(fs.existsSync(path.join(userDataDir, ROOT_MIGRATION_PREVIOUS_JOURNAL_FILE)), true);
  fs.writeFileSync(path.join(userDataDir, ROOT_MIGRATION_JOURNAL_FILE), "not-json");
  assert.equal(new RootConfigMigrationController({ userDataDir }).recover().status, "switched");
  assert.equal(fs.readFileSync(path.join(target.vaultRoot, "vaultRoot.txt"), "utf8"), "vaultRoot");
});

test("control-file replacement flushes the temporary file before rename and fails before publishing on flush failure", () => {
  const root = temporaryRoot();
  const userDataDir = path.join(root, "user-data");
  const source = sourceRoots(root);
  const target = targetRoots(root);
  const priorPointer = { version: 1, root: source.vaultRoot, roots: { model: source.modelRoot, media: source.mediaRoot } };
  writePointer(userDataDir, priorPointer);
  const flushed = [];
  assert.throws(() => new RootConfigMigrationController({
    userDataDir,
    flushFile: (candidate) => { flushed.push(candidate); throw new Error("flush_interrupted"); },
  }).migrate({ target }), /flush_interrupted/);
  assert.equal(flushed.length, 1);
  assert.match(flushed[0], /\.tmp-/);
  assert.equal(fs.existsSync(path.join(userDataDir, ROOT_MIGRATION_JOURNAL_FILE)), false);
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(userDataDir, DATA_ROOT_POINTER_FILE), "utf8")), priorPointer);
});

test("recovery rechecks copied targets before switching the pointer", () => {
  const root = temporaryRoot();
  const userDataDir = path.join(root, "user-data");
  const source = sourceRoots(root);
  const target = targetRoots(root);
  writePointer(userDataDir, { version: 1, root: source.vaultRoot, roots: { model: source.modelRoot, media: source.mediaRoot } });
  let copies = 0;
  const interrupted = new RootConfigMigrationController({
    userDataDir,
    copyTree: (from, staging) => { copies += 1; if (copies === 2) throw new Error("later_interrupted"); fs.cpSync(from, staging, { recursive: true }); },
  });
  assert.throws(() => interrupted.migrate({ target }), /later_interrupted/);
  fs.appendFileSync(path.join(target.vaultRoot, "vaultRoot.txt"), "external-change");
  assert.throws(() => new RootConfigMigrationController({ userDataDir }).recover(), /target_verification_failed/);
  assert.equal(resolveRootConfig({ userDataDir }).config.vaultRoot, source.vaultRoot);
});

test("moving only the Vault leaves unchanged Model and Media roles out of copy verification", () => {
  const root = temporaryRoot();
  const userDataDir = path.join(root, "user-data");
  const source = sourceRoots(root);
  const target = { vaultRoot: path.join(root, "target-vault"), modelRoot: source.modelRoot, mediaRoot: source.mediaRoot };
  writePointer(userDataDir, { version: 1, root: source.vaultRoot, roots: { model: source.modelRoot, media: source.mediaRoot } });
  const copies = [];
  const result = new RootConfigMigrationController({
    userDataDir,
    copyTree: (from, staging, roles) => { copies.push({ from, roles }); fs.cpSync(from, staging, { recursive: true }); },
  }).execute({ target });

  assert.equal(result.status, "switched");
  assert.deepEqual(copies, [{ from: source.vaultRoot, roles: ["vault"] }]);
  assert.deepEqual(resolveRootConfig({ userDataDir }).config, { version: 1, ...target, source: "versioned_pointer" });
});

test("an all-same-root migration is a nonpersistent noop", () => {
  const root = temporaryRoot();
  const userDataDir = path.join(root, "user-data");
  const source = sourceRoots(root);
  writePointer(userDataDir, { version: 1, root: source.vaultRoot, roots: { model: source.modelRoot, media: source.mediaRoot } });
  const result = new RootConfigMigrationController({ userDataDir, copyTree: () => { throw new Error("must_not_copy"); } }).execute({ target: source });

  assert.equal(result.status, "noop");
  assert.equal(fs.existsSync(path.join(userDataDir, ROOT_MIGRATION_JOURNAL_FILE)), false);
  assert.equal(fs.existsSync(path.join(userDataDir, ROOT_MIGRATION_PREVIOUS_JOURNAL_FILE)), false);
  assert.deepEqual(resolveRootConfig({ userDataDir }).config, { version: 1, ...source, source: "versioned_pointer" });
});

test("public plan preflight diagnose execute and recover APIs keep planning nonpersistent", () => {
  const root = temporaryRoot();
  const userDataDir = path.join(root, "user-data");
  const source = sourceRoots(root);
  const target = targetRoots(root);
  writePointer(userDataDir, { version: 1, root: source.vaultRoot, roots: { model: source.modelRoot, media: source.mediaRoot } });
  const controller = new RootConfigMigrationController({ userDataDir });

  const plan = controller.plan({ target });
  assert.notEqual(plan.operationId, controller.plan({ target }).operationId);
  assert.equal(plan.operations.length, 3);
  const preflight = controller.preflight({ target });
  assert.equal(preflight.operations.length, 3);
  assert.equal(fs.existsSync(path.join(userDataDir, ROOT_MIGRATION_JOURNAL_FILE)), false);
  assert.equal(controller.diagnose().mode, "configured");
  assert.equal(controller.execute({ target }).status, "switched");
  assert.equal(controller.recover().status, "switched");
});
