"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const { DeferredRuntimeRegistry } = require("../src/deferred-runtime-registry.cjs");

test("initializes each named runtime once and exposes no implicit construction", () => {
  let created = 0;
  const registry = new DeferredRuntimeRegistry({
    voice: { create: () => ({ id: ++created }) },
  });
  assert.equal(registry.get("voice"), null);
  const runtime = registry.initialize("voice");
  assert.equal(registry.initialize("voice"), runtime);
  assert.equal(registry.get("voice"), runtime);
  assert.equal(created, 1);
});

test("failed and invalid factories publish no partial value and can retry", () => {
  let attempt = 0;
  const registry = new DeferredRuntimeRegistry({
    vision: { create: () => { if (++attempt === 1) throw new Error("capture_unavailable"); return { attempt }; } },
  });
  assert.throws(() => registry.initialize("vision"), /capture_unavailable/);
  assert.equal(registry.get("vision"), null);
  assert.deepEqual(registry.initialize("vision"), { attempt: 2 });

  for (const invalid of [null, undefined, Promise.resolve({})]) {
    const invalidRegistry = new DeferredRuntimeRegistry({ runtime: { create: () => invalid } });
    assert.throws(() => invalidRegistry.initialize("runtime"), /deferred_runtime_factory_result_invalid/);
    assert.equal(invalidRegistry.get("runtime"), null);
  }
});

test("dispose clears identity before invoking its bounded cleanup and is idempotent", () => {
  const events = [];
  const runtime = { id: "voice" };
  let registry;
  registry = new DeferredRuntimeRegistry({
    voice: {
      create: () => runtime,
      dispose: (value) => events.push([value, registry.get("voice")]),
    },
  });
  registry.initialize("voice");
  assert.equal(registry.dispose("voice"), true);
  assert.equal(registry.dispose("voice"), false);
  assert.deepEqual(events, [[runtime, null]]);
});

test("async cleanup remains observable to the application lifecycle", async () => {
  let resolveCleanup;
  const registry = new DeferredRuntimeRegistry({
    runtime: {
      create: () => ({}),
      dispose: () => new Promise((resolve) => { resolveCleanup = resolve; }),
    },
  });
  registry.initialize("runtime");
  const disposal = registry.dispose("runtime");
  assert.equal(typeof disposal.then, "function");
  resolveCleanup();
  assert.equal(await disposal, true);
});

test("unknown names and malformed definitions fail closed", () => {
  assert.throws(() => new DeferredRuntimeRegistry(), /deferred_runtime_definitions_invalid/);
  for (const definitions of [
    { X: { create() {} } },
    { runtime: {} },
    { runtime: { create() {}, dispose: true } },
  ]) assert.throws(() => new DeferredRuntimeRegistry(definitions), /deferred_runtime_definition_invalid/);
  const registry = new DeferredRuntimeRegistry({ runtime: { create: () => ({}) } });
  assert.throws(() => registry.get("missing"), /deferred_runtime_unknown/);
  assert.throws(() => registry.initialize("missing"), /deferred_runtime_unknown/);
  assert.throws(() => registry.dispose("missing"), /deferred_runtime_unknown/);
});
