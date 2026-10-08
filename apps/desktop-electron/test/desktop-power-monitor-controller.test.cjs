"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const { DesktopPowerMonitorController } = require("../src/desktop-power-monitor-controller.cjs");

function fixture({ failOn = "" } = {}) {
  const events = [];
  const listeners = new Map();
  let idleSeconds = 31;
  const powerMonitor = {
    on(name, listener) {
      events.push(["on", name]);
      if (name === failOn) throw new Error("listener_install_failed");
      listeners.set(name, listener);
    },
    removeListener(name, listener) {
      events.push(["remove", name, listeners.get(name) === listener]);
      listeners.delete(name);
    },
    getSystemIdleTime() {
      events.push(["idle", idleSeconds]);
      return idleSeconds;
    },
  };
  const controller = new DesktopPowerMonitorController({
    powerMonitor,
    onLock: () => events.push("lock"),
    onUnlock: () => events.push("unlock"),
    onSuspend: () => events.push("suspend"),
    onResume: () => events.push("resume"),
  });
  return {
    controller,
    events,
    listeners,
    emit: (name) => listeners.get(name)?.(),
    setIdleSeconds: (value) => { idleSeconds = value; },
  };
}

test("installs the four fixed native lifecycle projections exactly once", () => {
  const value = fixture();
  assert.equal(value.controller.install(), true);
  assert.equal(value.controller.install(), false);
  assert.deepEqual(value.events, [
    ["on", "lock-screen"],
    ["on", "unlock-screen"],
    ["on", "suspend"],
    ["on", "resume"],
  ]);
  for (const name of ["lock-screen", "unlock-screen", "suspend", "resume"]) value.emit(name);
  assert.deepEqual(value.events.slice(-4), ["lock", "unlock", "suspend", "resume"]);
});

test("delegates every idle-time read without caching platform state", () => {
  const value = fixture();
  assert.equal(value.controller.getSystemIdleTime(), 31);
  value.setIdleSeconds(47);
  assert.equal(value.controller.getSystemIdleTime(), 47);
  assert.deepEqual(value.events, [["idle", 31], ["idle", 47]]);
});

test("dispose removes only owned listeners in reverse order and is idempotent", () => {
  const value = fixture();
  value.controller.install();
  assert.equal(value.controller.dispose(), true);
  assert.equal(value.controller.dispose(), false);
  assert.deepEqual(value.events.slice(-4), [
    ["remove", "resume", true],
    ["remove", "suspend", true],
    ["remove", "unlock-screen", true],
    ["remove", "lock-screen", true],
  ]);
  assert.equal(value.listeners.size, 0);
});

test("partial native listener setup rolls back before propagating failure", () => {
  const value = fixture({ failOn: "suspend" });
  assert.throws(() => value.controller.install(), /listener_install_failed/);
  assert.equal(value.listeners.size, 0);
  assert.deepEqual(value.events, [
    ["on", "lock-screen"],
    ["on", "unlock-screen"],
    ["on", "suspend"],
    ["remove", "unlock-screen", true],
    ["remove", "lock-screen", true],
  ]);
});

test("constructor rejects incomplete platform or projection ports", () => {
  assert.throws(() => new DesktopPowerMonitorController({}), /desktop_power_monitor_options_invalid/);
});
