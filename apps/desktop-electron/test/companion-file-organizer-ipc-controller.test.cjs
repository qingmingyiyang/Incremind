const test = require("node:test");
const assert = require("node:assert/strict");

const {
  CHANNELS,
  CompanionFileOrganizerIpcController,
} = require("../src/companion/file-organizer-ipc-controller.cjs");

function harness({ visible = true, focused = true } = {}) {
  const handlers = new Map();
  const calls = [];
  const mainWindow = { isVisible: () => visible, isFocused: () => focused };
  const organizer = {
    preview: (source, target) => ({ source, target, plan_id: "plan-1" }),
    execute: (planId) => {
      calls.push(["execute", planId]);
      return { status: "completed", operation_id: "operation-1", moved: 2 };
    },
    history: () => [{ operation_id: "operation-1" }],
    undo: (operationId) => ({ status: "undone", operation_id: operationId }),
  };
  const dialog = {
    showOpenDialog: async () => ({ canceled: true, filePaths: [] }),
    showMessageBox: async () => ({ response: 0 }),
  };
  const controller = new CompanionFileOrganizerIpcController({
    ipcMain: {
      handle: (channel, handler) => handlers.set(channel, handler),
      removeHandler: (channel) => handlers.delete(channel),
    },
    dialog,
    requireMainRenderer: (event) => {
      calls.push(["authorize", event.sender.id]);
      if (event.sender.id !== 7) throw new Error("ipc_main_sender_rejected");
    },
    mainWindowProvider: () => mainWindow,
    organizerProvider: () => organizer,
    onExecuted: (result) => calls.push(["feedback", result.operation_id]),
  });
  return { calls, controller, dialog, handlers, mainWindow, organizer };
}

const mainEvent = { sender: { id: 7 } };

test("installs and disposes the fixed file organizer IPC allowlist", () => {
  const { controller, handlers } = harness();
  assert.equal(controller.install(), true);
  assert.deepEqual([...handlers.keys()], CHANNELS);
  assert.equal(controller.install(), false);
  assert.equal(controller.dispose(), true);
  assert.equal(handlers.size, 0);
  assert.equal(controller.dispose(), false);
});

test("preview owns both native directory selections and never accepts renderer paths", async () => {
  const { controller, dialog, organizer } = harness();
  const selections = [
    { canceled: false, filePaths: ["C:\\Source"] },
    { canceled: false, filePaths: ["D:\\Target"] },
  ];
  const options = [];
  dialog.showOpenDialog = async (_window, value) => {
    options.push(value);
    return selections.shift();
  };
  const previewCalls = [];
  organizer.preview = (...args) => {
    previewCalls.push(args);
    return { plan_id: "plan-1" };
  };
  assert.deepEqual(await controller.preview(mainEvent), { status: "preview", preview: { plan_id: "plan-1" } });
  assert.deepEqual(previewCalls, [["C:\\Source", "D:\\Target"]]);
  assert.deepEqual(options.map((value) => value.properties), [
    ["openDirectory", "dontAddToRecent"],
    ["openDirectory", "createDirectory", "dontAddToRecent"],
  ]);
});

test("preview cancellation and unfocused windows fail before organizer access", async () => {
  const cancelled = harness();
  assert.deepEqual(await cancelled.controller.preview(mainEvent), { status: "cancelled" });
  const unfocused = harness({ focused: false });
  await assert.rejects(() => unfocused.controller.preview(mainEvent), /companion_file_organizer_unavailable/);
});

test("execute validates the exact payload and requires native confirmation before mutation", async () => {
  const { calls, controller, dialog } = harness();
  await assert.rejects(() => controller.execute(mainEvent, { plan_id: "plan-1", path: "C:\\Injected" }), /payload_rejected/);
  assert.equal(calls.some(([kind]) => kind === "execute"), false);
  assert.deepEqual(await controller.execute(mainEvent, { plan_id: "plan-1" }), { status: "cancelled" });
  assert.equal(calls.some(([kind]) => kind === "execute"), false);
  dialog.showMessageBox = async (_window, options) => {
    calls.push(["confirm", options]);
    return { response: 1 };
  };
  assert.deepEqual(await controller.execute(mainEvent, { plan_id: "plan-1" }), {
    status: "completed", operation_id: "operation-1", moved: 2,
  });
  assert.deepEqual(calls.slice(-3).map(([kind]) => kind), ["confirm", "execute", "feedback"]);
  const confirmation = calls.find(([kind]) => kind === "confirm")[1];
  assert.deepEqual(confirmation.buttons, ["取消", "确认移动"]);
  assert.equal(confirmation.defaultId, 0);
  assert.equal(confirmation.cancelId, 0);
});

test("history and undo remain main-renderer-only with strict operation ids", () => {
  const { controller } = harness();
  assert.deepEqual(controller.history(mainEvent), {
    status: "ready", operations: [{ operation_id: "operation-1" }],
  });
  assert.throws(() => controller.undo(mainEvent, { operation_id: "operation-1", extra: true }), /payload_rejected/);
  assert.deepEqual(controller.undo(mainEvent, { operation_id: "operation-1" }), {
    status: "undone", operation_id: "operation-1",
  });
  assert.throws(() => controller.history({ sender: { id: 99 } }), /ipc_main_sender_rejected/);
});
