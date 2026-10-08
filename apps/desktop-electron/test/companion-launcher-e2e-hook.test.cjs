"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { createTemporaryRootTracker } = require("./support/temporary-root.cjs");

const {
  ENTRY_NAME,
  MODE,
  MODE_KEY,
  RECEIPT_NAME,
  SWITCH,
  launcherE2ELaunchArgs,
  recordCompanionLauncherE2EReceipt,
  resolveCompanionLauncherE2E,
  seedCompanionLauncherE2ETarget,
} = require("../src/companion/launcher-e2e-hook.cjs");

const temporaryRoot = createTemporaryRootTracker(test);

function fixture(overrides = {}) {
  const root = temporaryRoot("chriptmas-launcher-e2e-");
  return {
    argv: [SWITCH],
    env: { [MODE_KEY]: MODE },
    executablePath: path.join(root, "Chriptmas OS.exe"),
    userDataRoot: path.join(root, "user-data"),
    ...overrides,
  };
}

test("launcher E2E hook requires one exact argv and environment gate", () => {
  const value = fixture();
  assert.equal(resolveCompanionLauncherE2E({ ...value, argv: [] }), null);
  assert.equal(resolveCompanionLauncherE2E({ ...value, env: {} }), null);
  assert.equal(resolveCompanionLauncherE2E({ ...value, env: { ...value.env, CHRIPTMAS_E2E_COMPANION_LAUNCHER_PATH: "C:/unsafe.exe" } }), null);
  assert.equal(resolveCompanionLauncherE2E({ ...value, executablePath: "relative.exe" }), null);
  assert.equal(resolveCompanionLauncherE2E({ ...value, executablePath: path.join(path.dirname(value.executablePath), "python.exe") }).entryName, ENTRY_NAME);
});

test("launcher E2E launch argument is confined to the exact dual-gated self target", () => {
  const target = resolveCompanionLauncherE2E(fixture());
  assert.deepEqual(launcherE2ELaunchArgs({ kind: "program", path: target.targetPath }, target), [`--user-data-dir=${target.userDataRoot}`]);
  assert.deepEqual(launcherE2ELaunchArgs({ kind: "program", path: `${target.targetPath}.other` }, target), []);
  assert.deepEqual(launcherE2ELaunchArgs({ kind: "bookmark", path: target.targetPath }, target), []);
  assert.deepEqual(launcherE2ELaunchArgs({ kind: "program", path: target.targetPath }, { ...target, userDataRoot: "relative" }), []);
});

test("launcher E2E hook seeds only the candidate target and never accepts a caller path", () => {
  const target = resolveCompanionLauncherE2E(fixture());
  const calls = [];
  const controller = {
    ensureProgram: (value) => { calls.push(value); return { id: "program:00000000-0000-4000-8000-000000000001", kind: "program", name: value.name, target: path.basename(value.selectedPath) }; },
  };
  const seeded = seedCompanionLauncherE2ETarget(controller, target);
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], { name: ENTRY_NAME, selectedPath: target.targetPath });
  assert.equal(seeded.target, path.basename(target.targetPath));
  assert.equal(seedCompanionLauncherE2ETarget({ ensureProgram: () => null }, target), null);
});

test("launcher E2E receipt is fixed, bounded and written only below the owned user-data root", () => {
  const target = resolveCompanionLauncherE2E(fixture());
  assert.equal(recordCompanionLauncherE2EReceipt(target), true);
  assert.deepEqual(JSON.parse(fs.readFileSync(target.receiptPath, "utf8")), { marker: "candidate-self", sequence: 1 });
  assert.equal(path.basename(target.receiptPath), RECEIPT_NAME);
  assert.equal(recordCompanionLauncherE2EReceipt({ userDataRoot: path.dirname(target.receiptPath), receiptPath: path.join(path.dirname(target.receiptPath), "outside.json") }), false);
  const receiptStat = fs.lstatSync(target.receiptPath);
  assert.equal(recordCompanionLauncherE2EReceipt({ ...target, receiptPath: target.receiptPath }, {
    existsSync: () => true,
    lstatSync: () => ({ isDirectory: () => false, isFile: () => receiptStat.isFile(), isSymbolicLink: () => true }),
  }), false);
});
