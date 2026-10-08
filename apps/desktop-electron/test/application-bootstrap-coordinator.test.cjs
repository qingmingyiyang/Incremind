"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const { ApplicationBootstrapCoordinator } = require("../src/application-bootstrap-coordinator.cjs");

function fixture({ enabled = true, runtime, steps } = {}) {
  const events = [];
  const listeners = new Map();
  let whenReadyCalls = 0;
  const app = {
    whenReady: () => { whenReadyCalls += 1; return Promise.resolve(); },
    on: (name, listener) => { events.push(["on", name]); listeners.set(name, listener); },
    removeListener: (name, listener) => { events.push(["remove", name]); if (listeners.get(name) === listener) listeners.delete(name); },
  };
  const coordinator = new ApplicationBootstrapCoordinator({
    app,
    enabledProvider: () => enabled,
    beginStartupFeedback: () => events.push("feedback"),
    startRequiredRuntime: runtime || (() => events.push("runtime")),
    onRequiredRuntimeFailure: (error) => events.push(["runtime-failed", error.message]),
    steps: steps || [
      { name: "optional", optional: true, run: () => { events.push("optional"); throw new Error("optional-down"); }, onError: (error) => events.push(["optional-failed", error.message]) },
      { name: "required", run: () => events.push("required") },
    ],
    onReady: () => events.push("ready"),
    onActivate: () => events.push("activate"),
  });
  return { coordinator, events, listeners, get whenReadyCalls() { return whenReadyCalls; } };
}

test("successful bootstrap preserves feedback runtime optional required ready and activate order", async () => {
  const value = fixture();
  assert.deepEqual(await value.coordinator.start(), { status: "ready" });
  assert.equal(value.coordinator.ready, true);
  assert.deepEqual(value.events, [
    "feedback",
    "runtime",
    "optional",
    ["optional-failed", "optional-down"],
    "required",
    "ready",
    ["on", "activate"],
  ]);
  value.listeners.get("activate")();
  assert.equal(value.events.at(-1), "activate");
});

test("single-instance rejection performs no startup work", async () => {
  const value = fixture({ enabled: false });
  assert.deepEqual(await value.coordinator.start(), { status: "ignored", reason: "single_instance_lock_unavailable" });
  assert.equal(value.coordinator.ready, false);
  assert.deepEqual(value.events, []);
});

test("required runtime failure uses the fixed failure port and stops later steps", async () => {
  const value = fixture({ runtime: () => { value.events.push("runtime"); throw new Error("sidecar-down"); } });
  assert.deepEqual(await value.coordinator.start(), { status: "failed", stage: "required-runtime" });
  assert.deepEqual(value.events, ["feedback", "runtime", ["runtime-failed", "sidecar-down"]]);
  assert.equal(value.coordinator.ready, false);
});

test("a required desktop step rejects without publishing ready", async () => {
  const value = fixture({ steps: [{ name: "required", run: () => { throw new Error("window-down"); } }] });
  await assert.rejects(value.coordinator.start(), /window-down/);
  assert.equal(value.coordinator.ready, false);
  assert.deepEqual(value.events, ["feedback", "runtime"]);
});

test("start and install are independently idempotent", async () => {
  let release;
  const runtime = () => new Promise((resolve) => { release = resolve; });
  const value = fixture({ runtime, steps: [] });
  const installed = value.coordinator.install();
  assert.equal(value.coordinator.install(), installed);
  await new Promise((resolve) => setImmediate(resolve));
  const started = value.coordinator.start();
  assert.equal(value.coordinator.start(), started);
  release();
  assert.deepEqual(await installed, { status: "ready" });
  assert.equal(value.whenReadyCalls, 1);
});

test("dispose removes only the installed activate listener", async () => {
  const value = fixture({ steps: [] });
  await value.coordinator.start();
  assert.equal(value.coordinator.dispose(), true);
  assert.equal(value.coordinator.dispose(), false);
  assert.deepEqual(value.events.slice(-2), [["on", "activate"], ["remove", "activate"]]);
});

test("constructor rejects incomplete duplicate or invalid step contracts", () => {
  assert.throws(() => new ApplicationBootstrapCoordinator(), /application_bootstrap_options_invalid/);
  const base = {
    app: { whenReady() {}, on() {}, removeListener() {} },
    enabledProvider() {}, beginStartupFeedback() {}, startRequiredRuntime() {}, onRequiredRuntimeFailure() {}, onReady() {}, onActivate() {},
  };
  assert.throws(() => new ApplicationBootstrapCoordinator({ ...base, steps: [{ name: "same", run() {} }, { name: "same", run() {} }] }), /application_bootstrap_step_invalid/);
  assert.throws(() => new ApplicationBootstrapCoordinator({ ...base, steps: [{ name: "optional", run() {}, optional: true }] }), /application_bootstrap_optional_error_handler_required/);
});
