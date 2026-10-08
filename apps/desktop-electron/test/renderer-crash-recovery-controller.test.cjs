"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const { RendererCrashRecoveryController } = require("../src/renderer-crash-recovery-controller.cjs");
const mainSource = fs.readFileSync(path.join(__dirname, "..", "src", "main.cjs"), "utf8");

function fixture({ quitting = false, reloadError = null } = {}) {
  const listeners = new Map();
  const logs = [];
  const webContents = {
    destroyed: false,
    reloadCalls: 0,
    on: (event, listener) => listeners.set(event, listener),
    removeListener: (event, listener) => {
      if (listeners.get(event) === listener) listeners.delete(event);
    },
    isDestroyed() { return this.destroyed; },
    reload() {
      this.reloadCalls += 1;
      if (reloadError) throw reloadError;
    },
    emitGone: (reason) => listeners.get("render-process-gone")?.({}, { reason }),
    destroy: () => {
      webContents.destroyed = true;
      listeners.get("destroyed")?.();
    },
  };
  const controller = new RendererCrashRecoveryController({
    isApplicationQuitting: () => quitting,
    log: (message) => logs.push(message),
  });
  return { controller, logs, webContents };
}

test("a known abnormal renderer exit reloads the watched renderer once", () => {
  const value = fixture();
  assert.equal(value.controller.watch(value.webContents), true);
  assert.equal(value.controller.watch(value.webContents), false);

  value.webContents.emitGone("crashed");
  assert.equal(value.webContents.reloadCalls, 1);
  assert.deepEqual(value.logs, ["[renderer] process gone (crashed); reloading renderer once"]);
});

test("a second abnormal renderer exit fails closed without a crash loop", () => {
  const value = fixture();
  value.controller.watch(value.webContents);
  value.webContents.emitGone("oom");
  value.webContents.emitGone("crashed");

  assert.equal(value.webContents.reloadCalls, 1);
  assert.deepEqual(value.logs, [
    "[renderer] process gone (oom); reloading renderer once",
    "[renderer] process gone (crashed); recovery suppressed: reload already used",
  ]);
});

test("normal destruction and clean renderer exit never reload", () => {
  const clean = fixture();
  clean.controller.watch(clean.webContents);
  clean.webContents.emitGone("clean-exit");
  assert.equal(clean.webContents.reloadCalls, 0);
  assert.deepEqual(clean.logs, []);

  const destroyed = fixture();
  destroyed.controller.watch(destroyed.webContents);
  destroyed.webContents.destroy();
  destroyed.webContents.emitGone("crashed");
  assert.equal(destroyed.webContents.reloadCalls, 0);
  assert.deepEqual(destroyed.logs, []);
});

test("application quit, unknown exit reasons, and failed reloads fail closed", () => {
  const quitting = fixture({ quitting: true });
  quitting.controller.watch(quitting.webContents);
  quitting.webContents.emitGone("killed");
  assert.equal(quitting.webContents.reloadCalls, 0);
  assert.deepEqual(quitting.logs, ["[renderer] process gone (killed); recovery suppressed: application quitting"]);

  const unknown = fixture();
  unknown.controller.watch(unknown.webContents);
  unknown.webContents.emitGone("unexpected-value");
  assert.equal(unknown.webContents.reloadCalls, 0);
  assert.deepEqual(unknown.logs, ["[renderer] process gone (unexpected-value); recovery suppressed: unknown reason"]);

  const failed = fixture({ reloadError: new Error("reload\nfailed") });
  failed.controller.watch(failed.webContents);
  failed.webContents.emitGone("abnormal-exit");
  assert.equal(failed.webContents.reloadCalls, 1);
  assert.deepEqual(failed.logs, ["[renderer] process gone (abnormal-exit); reload failed: reload failed"]);
});

test("unwatch removes the event authority and constructor validates dependencies", () => {
  const value = fixture();
  value.controller.watch(value.webContents);
  assert.equal(value.controller.unwatch(value.webContents), true);
  assert.equal(value.controller.unwatch(value.webContents), false);
  value.webContents.emitGone("crashed");
  assert.equal(value.webContents.reloadCalls, 0);
  assert.throws(() => new RendererCrashRecoveryController(), /renderer_crash_recovery_options_invalid/);
  assert.throws(() => value.controller.watch({}), /renderer_crash_recovery_web_contents_invalid/);
});

test("the main workspace is the only production renderer attached to the recovery controller", () => {
  assert.match(mainSource, /const \{ RendererCrashRecoveryController \} = require\("\.\/renderer-crash-recovery-controller\.cjs"\)/);
  const mainWindow = mainSource.slice(mainSource.indexOf("function createMainWindow"), mainSource.indexOf("function createPetWindow"));
  assert.match(mainWindow, /desktopWindowRegistry\.register\("main", createdMainWindow\);\s*rendererCrashRecoveryController\.watch\(createdMainWindow\.webContents\);/s);
  assert.doesNotMatch(mainSource, /rendererCrashRecoveryController\.watch\(createdPetWindow\.webContents\)/);
});
