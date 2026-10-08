const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const test = require("node:test");

const { runPackagedShutdownHandshake } = require("../scripts/e2e-shutdown-handshake.cjs");

function makeSession() {
  const child = new EventEmitter();
  child.exitCode = null;
  child.signalCode = null;
  return {
    child,
    sidecarPid: 4242,
  };
}

test("records a production native-menu app.quit trigger and normal shutdown evidence", async () => {
  const session = makeSession();
  let tick = 0;
  let triggers = 0;
  const evidence = await runPackagedShutdownHandshake({
    session,
    now: () => ++tick,
    triggerShutdown: async () => { triggers += 1; return { status: "opened", action_id: "app.quit" }; },
    processIsAlive: () => false,
    waitFor: async (probe) => {
      try { probe(); } catch {}
      session.child.exitCode = 0;
      session.child.emit("exit", 0, null);
      return probe();
    },
  });
  assert.equal(triggers, 1);
  assert.deepEqual(evidence.observation_order, ["sidecar_absent", "electron_exit"]);
  assert.equal(evidence.trigger_route, "pet_native_menu_action_registry_app_quit");
  assert.equal(evidence.child_exit_code, 0);
  assert.equal(evidence.child_exit_signal, null);
  assert.ok(evidence.shutdown_trigger_to_sidecar_absent_ms >= 0);
  assert.ok(evidence.shutdown_trigger_to_electron_exit_ms >= evidence.shutdown_trigger_to_sidecar_absent_ms);
});

test("records Electron-first observation without claiming a causal ordering", async () => {
  const session = makeSession();
  let sidecarAlive = true;
  const evidence = await runPackagedShutdownHandshake({
      session,
      triggerShutdown: async () => ({ status: "opened", action_id: "app.quit" }),
      processIsAlive: () => sidecarAlive,
      waitFor: async (probe) => {
        session.child.exitCode = 0;
        session.child.emit("exit", 0, null);
        try { probe(); } catch {}
        sidecarAlive = false;
        return probe();
      },
    });
  assert.deepEqual(evidence.observation_order, ["electron_exit", "sidecar_absent"]);
});

test("requires an authenticated sidecar PID instead of silently weakening the Gate", async () => {
  const session = makeSession();
  session.sidecarPid = null;
  await assert.rejects(
    runPackagedShutdownHandshake({ session, triggerShutdown: async () => ({ status: "opened", action_id: "app.quit" }), waitFor: async () => true, processIsAlive: () => false }),
    /shutdown_handshake_sidecar_pid_invalid/,
  );
});

test("rejects a non-zero Electron exit even after both processes stop", async () => {
  const session = makeSession();
  await assert.rejects(
    runPackagedShutdownHandshake({
      session,
      triggerShutdown: async () => ({ status: "opened", action_id: "app.quit" }),
      processIsAlive: () => false,
      waitFor: async (probe) => {
        try { probe(); } catch {}
        session.child.exitCode = 9;
        session.child.emit("exit", 9, null);
        return probe();
      },
    }),
    /Electron normal shutdown exited unexpectedly: code=9 signal=none/,
  );
});

test("fails closed when the native action registry does not report app.quit", async () => {
  const session = makeSession();
  await assert.rejects(
    runPackagedShutdownHandshake({
      session,
      triggerShutdown: async () => ({ status: "opened", action_id: "companion.pet.hide" }),
      waitFor: async () => true,
      processIsAlive: () => false,
    }),
    /native menu shutdown action was not accepted/,
  );
});
