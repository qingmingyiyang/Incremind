"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const { DesktopWindowRegistry } = require("../src/desktop-window-registry.cjs");

function windowStub() {
  const listeners = new Map();
  return {
    destroyed: false,
    destroyCalls: 0,
    once: (event, listener) => listeners.set(event, listener),
    isDestroyed() { return this.destroyed; },
    destroy() { this.destroyCalls += 1; this.destroyed = true; listeners.get("closed")?.(); },
    closeEvent() { listeners.get("closed")?.(); },
  };
}

test("registry owns one identity for each fixed desktop window kind", () => {
  const registry = new DesktopWindowRegistry();
  const main = windowStub();
  const pet = windowStub();
  const overlay = windowStub();
  const startup = windowStub();
  registry.register("main", main);
  registry.register("pet", pet);
  registry.register("overlay", overlay);
  registry.register("startup", startup);
  assert.equal(registry.main, main);
  assert.equal(registry.pet, pet);
  assert.equal(registry.overlay, overlay);
  assert.equal(registry.startup, startup);
  assert.equal(registry.register("main", main), main);
});

test("a live identity cannot be silently replaced", () => {
  const registry = new DesktopWindowRegistry();
  registry.register("pet", windowStub());
  assert.throws(() => registry.register("pet", windowStub()), /desktop_window_already_registered:pet/);
});

test("stale closed callbacks never clear a later registered identity", () => {
  const registry = new DesktopWindowRegistry();
  const oldWindow = windowStub();
  const currentWindow = windowStub();
  const closed = [];
  registry.register("overlay", oldWindow, { onClosed: () => closed.push("old") });
  oldWindow.destroyed = true;
  registry.register("overlay", currentWindow, { onClosed: () => closed.push("current") });
  oldWindow.closeEvent();
  assert.equal(registry.overlay, currentWindow);
  assert.deepEqual(closed, []);
  currentWindow.closeEvent();
  assert.equal(registry.overlay, null);
  assert.deepEqual(closed, ["current"]);
});

test("identity-bound clear rejects an unrelated window", () => {
  const registry = new DesktopWindowRegistry();
  const main = windowStub();
  registry.register("main", main);
  assert.equal(registry.clear("main", windowStub()), false);
  assert.equal(registry.main, main);
  assert.equal(registry.clear("main", main), true);
  assert.equal(registry.main, null);
});

test("destroy is idempotent and clears both live and already destroyed windows", () => {
  const registry = new DesktopWindowRegistry();
  const startup = windowStub();
  registry.register("startup", startup);
  assert.equal(registry.destroy("startup"), true);
  assert.equal(startup.destroyCalls, 1);
  assert.equal(registry.startup, null);
  assert.equal(registry.destroy("startup"), false);
  const pet = windowStub();
  pet.destroyed = true;
  registry.register("pet", pet);
  assert.equal(registry.destroy("pet"), true);
  assert.equal(pet.destroyCalls, 0);
  assert.equal(registry.pet, null);
});

test("unknown kinds and incomplete windows fail closed", () => {
  const registry = new DesktopWindowRegistry();
  assert.throws(() => registry.register("other", windowStub()), /desktop_window_kind_invalid/);
  assert.throws(() => registry.register("main", {}), /desktop_window_invalid/);
  assert.throws(() => registry.register("main", windowStub(), { onClosed: true }), /desktop_window_closed_callback_invalid/);
  assert.throws(() => registry.clear("other"), /desktop_window_kind_invalid/);
});
