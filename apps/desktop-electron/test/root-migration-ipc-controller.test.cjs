const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { CHANNELS, RootMigrationIpcController, isExecuteRequest, isRootTarget } = require("../src/root-migration-ipc-controller.cjs");
const { RootConfigMigrationController } = require("../src/vault-root.cjs");

const target = Object.freeze({ vaultRoot: "C:\\next\\vault", modelRoot: "C:\\next\\vault", mediaRoot: "C:\\next\\vault" });
const event = Object.freeze({ trusted: true });

function harness({ confirm = 1, runExclusive = async (work) => ({ value: await work({ assertSourceQuiescent() {} }) }) } = {}) {
  const handlers = new Map();
  const removed = [];
  const calls = [];
  const controller = new RootMigrationIpcController({
    ipcMain: { handle(channel, handler) { handlers.set(channel, handler); }, removeHandler(channel) { removed.push(channel); } },
    dialog: {
      async showOpenDialog() { return { canceled: false, filePaths: ["C:\\selected"] }; },
      async showMessageBox(_window, options) { calls.push(["confirm", options]); return { response: confirm }; },
    },
    requireMainRenderer(value) { if (!value?.trusted) throw new Error("sender_rejected"); },
    mainWindowProvider() { return { isVisible: () => true, isFocused: () => true }; },
    controllerFactory(assertSourceQuiescent) {
      return {
        diagnose() { return { mode: "configured", configured: target, actual: { vault: { exists: true } } }; },
        preflight({ target: candidate }) { assertSourceQuiescent(); return { operationId: "root-123456789012-abcdef12", operations: [{ roles: ["vault"] }], target: candidate }; },
        execute({ target: candidate }) { assertSourceQuiescent(); return { status: "switched", operationId: "root-123456789013-abcdef12", target: candidate }; },
        recover() { assertSourceQuiescent(); return { status: "none" }; },
      };
    },
    runExclusive,
    now: () => 100,
  });
  controller.install();
  return { controller, handlers, removed, calls };
}

test("root migration IPC accepts only bounded root targets and execute DTOs", () => {
  assert.equal(isRootTarget(target), true);
  assert.equal(isRootTarget({ ...target, extra: true }), false);
  assert.equal(isRootTarget({ ...target, vaultRoot: "" }), false);
  assert.equal(isExecuteRequest({ operationId: "root-123456789012-abcdef12", target: { ...target, mediaRoot: "C:\\next\\media" } }), true);
  assert.equal(isExecuteRequest({ operationId: "root-123456789012-abcdef12", target }), true);
  assert.equal(isExecuteRequest({ operationId: "../root-123456789012-abcdef12", target }), false);
});

test("preflight is run inside the sidecar-exclusive boundary and execute needs a reviewed plan", async () => {
  const order = [];
  const value = harness({ runExclusive: async (work) => { order.push("offline"); return { value: await work({ assertSourceQuiescent() { order.push("quiescent"); } }) }; } });
  const preflight = await value.handlers.get(CHANNELS.preflight)(event, target);
  assert.equal(preflight.restart_status, "ready");
  assert.deepEqual(order, ["offline", "quiescent"]);
  const result = await value.handlers.get(CHANNELS.execute)(event, { operationId: preflight.operationId, target });
  assert.equal(result.status, "switched");
  assert.equal(value.calls[0][1].buttons[1], "确认迁移");
  await assert.rejects(value.handlers.get(CHANNELS.execute)(event, { operationId: "root-123456789999-abcdef12", target }), /preflight_expired/);
});

test("cancelled confirmation leaves migration data untouched and dispose owns all channels", async () => {
  const value = harness({ confirm: 0 });
  const preflight = await value.handlers.get(CHANNELS.preflight)(event, target);
  assert.deepEqual(await value.handlers.get(CHANNELS.execute)(event, { operationId: preflight.operationId, target }), { status: "cancelled" });
  assert.equal(value.controller.dispose(), true);
  assert.deepEqual(removedSorted(value.removed), Object.values(CHANNELS).sort());
});

test("production root controller preflight output can pass the IPC confirmation boundary and switch a synthetic root", async () => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-root-ipc-"));
  try {
    const source = path.join(temporary, "vault");
    const next = path.join(temporary, "parent", "Chriptmas-data-reviewed");
    fs.mkdirSync(source, { recursive: true });
    fs.mkdirSync(path.dirname(next), { recursive: true });
    fs.writeFileSync(path.join(source, "note.txt"), "safe synthetic content");
    const handlers = new Map();
    const controller = new RootMigrationIpcController({
      ipcMain: { handle(channel, handler) { handlers.set(channel, handler); }, removeHandler() {} },
      dialog: {
        async showOpenDialog() { return { canceled: true, filePaths: [] }; },
        async showMessageBox() { return { response: 1 }; },
      },
      requireMainRenderer(value) { assert.equal(value, event); },
      mainWindowProvider: () => ({ isVisible: () => true, isFocused: () => true }),
      controllerFactory: (assertSourceQuiescent) => new RootConfigMigrationController({ userDataDir: temporary, assertSourceQuiescent }),
      runExclusive: async (work) => ({ value: await work({ assertSourceQuiescent() {} }) }),
    });
    controller.install();
    const migrationTarget = { vaultRoot: next, modelRoot: next, mediaRoot: next };
    const preflight = await handlers.get(CHANNELS.preflight)(event, migrationTarget);
    assert.match(preflight.operationId, /^root-[0-9]+-[0-9a-f-]+$/);
    const executed = await handlers.get(CHANNELS.execute)(event, { operationId: preflight.operationId, target: migrationTarget });
    assert.equal(executed.status, "switched");
    assert.deepEqual(executed.observed.configured, migrationTarget);
    assert.equal(fs.readFileSync(path.join(next, "note.txt"), "utf8"), "safe synthetic content");
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
});

function removedSorted(items) { return [...items].sort(); }
