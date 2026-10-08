const assert = require("node:assert/strict");
const test = require("node:test");

const { CHANNEL, VaultRestoreIpcController, isRestoreRequest } = require("../src/vault-restore-ipc-controller.cjs");

const event = Object.freeze({ trusted: true });
const request = Object.freeze({ snapshot_id: "snap-safe-1", rollback_id: "rb-012345abcdef" });

function response({ ok = true, status = 200, payload = { status: "prepared_restart_required", operation_id: "restore-1" } } = {}) {
  return { ok, status, async json() { return payload; } };
}

function harness({
  confirmationResponse = 1,
  visible = true,
  focused = true,
  session = { origin: "http://127.0.0.1:11495", secret: "session-secret" },
  recovery = { async adopt(operationId) { return { status: "adopted", operation_id: operationId }; } },
  fetchResponse = response(),
  fetchError = null,
  stopError = null,
} = {}) {
  const calls = [];
  const handlers = new Map();
  const removed = [];
  const mainWindow = { isVisible: () => visible, isFocused: () => focused };
  const controller = new VaultRestoreIpcController({
    ipcMain: {
      handle(channel, handler) {
        assert.equal(handlers.has(channel), false, `duplicate ${channel}`);
        handlers.set(channel, handler);
      },
      removeHandler(channel) {
        removed.push(channel);
        handlers.delete(channel);
      },
    },
    dialog: {
      async showMessageBox(window, options) {
        calls.push(["dialog", window, options]);
        return { response: options.type === "warning" ? confirmationResponse : 0 };
      },
    },
    requireMainRenderer(value) {
      calls.push(["guard", value]);
      if (value?.trusted !== true) throw new Error("ipc_main_sender_rejected");
    },
    mainWindowProvider() {
      calls.push(["window"]);
      return mainWindow;
    },
    sessionProvider() {
      calls.push(["session"]);
      return session;
    },
    recoveryProvider() {
      calls.push(["recovery"]);
      return recovery;
    },
    async sidecarStop() {
      calls.push(["stop"]);
      if (stopError) throw stopError;
    },
    scheduleRelaunch() {
      calls.push(["relaunch"]);
    },
    sessionHeader: "X-Chriptmas-Session",
    async fetchImpl(url, options) {
      calls.push(["fetch", url, options]);
      if (fetchError) throw fetchError;
      return fetchResponse;
    },
    timeoutSignal(milliseconds) {
      const signal = { milliseconds };
      calls.push(["timeout", milliseconds]);
      return signal;
    },
  });
  controller.install();
  return { calls, controller, handlers, mainWindow, removed };
}

test("restore request accepts only the exact bounded DTO", () => {
  assert.equal(isRestoreRequest(request), true);
  for (const payload of [
    null,
    [],
    {},
    { ...request, extra: true },
    { ...request, snapshot_id: "../escape" },
    { ...request, rollback_id: "rb-not-hex" },
  ]) assert.equal(isRestoreRequest(payload), false, JSON.stringify(payload));
});

test("controller owns one idempotent channel and disposes it", () => {
  const value = harness();
  assert.deepEqual([...value.handlers.keys()], [CHANNEL]);
  assert.equal(value.controller.install(), false);
  assert.equal(value.controller.dispose(), true);
  assert.equal(value.controller.dispose(), false);
  assert.deepEqual(value.removed, [CHANNEL]);
  assert.equal(value.handlers.size, 0);
});

test("sender payload and focused window are checked before confirmation or runtime access", async () => {
  const untrusted = harness();
  await assert.rejects(untrusted.handlers.get(CHANNEL)({ trusted: false }, request), /ipc_main_sender_rejected/);
  assert.deepEqual(untrusted.calls.map(([name]) => name), ["guard"]);

  const invalid = harness();
  await assert.rejects(invalid.handlers.get(CHANNEL)(event, { ...request, extra: true }), /payload_rejected/);
  assert.deepEqual(invalid.calls.map(([name]) => name), ["guard"]);

  for (const state of [{ visible: false }, { focused: false }]) {
    const value = harness(state);
    await assert.rejects(value.handlers.get(CHANNEL)(event, request), /window_required/);
    assert.deepEqual(value.calls.map(([name]) => name), ["guard", "window"]);
  }
});

test("native cancellation is inert and keeps the sidecar online", async () => {
  const value = harness({ confirmationResponse: 0 });
  assert.deepEqual(await value.handlers.get(CHANNEL)(event, request), { status: "cancelled" });
  assert.deepEqual(value.calls.map(([name]) => name), ["guard", "window", "dialog"]);
  const options = value.calls[2][2];
  assert.deepEqual(options.buttons, ["取消", "确认恢复并重启"]);
  assert.equal(options.defaultId, 0);
  assert.equal(options.cancelId, 0);
});

test("successful restore prepares with authenticated DTO then stops before offline adoption", async () => {
  const order = [];
  const recovery = {
    async adopt(operationId) {
      order.push(["adopt", operationId]);
      return { status: "adopted", operation_id: operationId };
    },
  };
  const value = harness({ recovery });
  const originalStop = value.controller.sidecarStop;
  value.controller.sidecarStop = async () => {
    order.push(["stop"]);
    await originalStop();
  };
  value.controller.scheduleRelaunch = () => {
    order.push(["relaunch"]);
    value.calls.push(["relaunch"]);
  };

  assert.deepEqual(await value.handlers.get(CHANNEL)(event, request), {
    status: "restored_relaunching",
    operation_id: "restore-1",
  });
  assert.deepEqual(order, [["stop"], ["adopt", "restore-1"], ["relaunch"]]);
  const fetchCall = value.calls.find(([name]) => name === "fetch");
  assert.equal(fetchCall[1], "http://127.0.0.1:11495/api/rebuild/memory-snapshots/snap-safe-1/rollback");
  assert.equal(fetchCall[2].method, "POST");
  assert.equal(fetchCall[2].headers["X-Chriptmas-Session"], "session-secret");
  assert.equal(fetchCall[2].body, JSON.stringify({ rollback_id: "rb-012345abcdef", confirm: true }));
  assert.equal(fetchCall[2].signal.milliseconds, 120000);
});

test("missing runtime failed prepare and stop failure never cross the adoption boundary", async () => {
  const missing = harness({ session: null });
  await assert.rejects(missing.handlers.get(CHANNEL)(event, request), /runtime_unavailable/);
  assert.equal(missing.calls.some(([name]) => name === "fetch"), false);

  const rejected = harness({ fetchResponse: response({ ok: false, status: 409, payload: { detail: "restore_conflict" } }) });
  await assert.rejects(rejected.handlers.get(CHANNEL)(event, request), /restore_conflict/);
  assert.equal(rejected.calls.some(([name]) => name === "stop"), false);

  let adopted = false;
  const stopped = harness({
    stopError: new Error("sidecar_stop_failed"),
    recovery: { async adopt() { adopted = true; } },
  });
  await assert.rejects(stopped.handlers.get(CHANNEL)(event, request), /sidecar_stop_failed/);
  assert.equal(adopted, false);
  assert.equal(stopped.calls.some(([name]) => name === "relaunch"), false);
});

test("adoption conflict preserves recovery evidence and schedules startup reconciliation", async () => {
  const value = harness({
    recovery: { async adopt() { throw new Error("vault_recovery_conflict"); } },
  });
  assert.deepEqual(await value.handlers.get(CHANNEL)(event, request), {
    status: "recovery_pending_relaunch",
    error: "vault_recovery_conflict",
  });
  const names = value.calls.map(([name]) => name);
  assert.ok(names.indexOf("stop") < names.lastIndexOf("dialog"));
  assert.ok(names.lastIndexOf("dialog") < names.indexOf("relaunch"));
  assert.equal(value.calls.filter(([name]) => name === "dialog").at(-1)[2].type, "error");
});
