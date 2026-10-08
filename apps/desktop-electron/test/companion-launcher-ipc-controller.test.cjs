const test = require("node:test");
const assert = require("node:assert/strict");

const { CHANNELS, CompanionLauncherIpcController } = require("../src/companion/launcher-ipc-controller.cjs");

function harness() {
  const handlers = new Map();
  const calls = [];
  const mainWindow = { name: "main" };
  const launcher = {
    list: () => ({ state: "ready", entries: [] }),
    addProgram: (payload) => ({ id: "program:1", ...payload }),
    addBookmark: (payload) => ({ id: "bookmark:1", ...payload }),
    rename: (payload) => payload,
    remove: ({ id }) => ({ status: "deleted", id }),
    launch: async ({ id }) => ({ status: "launched", id, name: "工作程序" }),
  };
  const dialog = { showOpenDialog: async () => ({ canceled: true, filePaths: [] }) };
  let activeLauncher = launcher;
  const controller = new CompanionLauncherIpcController({
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
    launcherProvider: () => activeLauncher,
    onLaunched: (result) => calls.push(["feedback", result.id]),
  });
  return { calls, controller, dialog, handlers, launcher, mainWindow, setLauncher: (value) => { activeLauncher = value; } };
}

const mainEvent = { sender: { id: 7 } };

test("installs and disposes the fixed launcher IPC allowlist", () => {
  const { controller, handlers } = harness();
  assert.equal(controller.install(), true);
  assert.deepEqual([...handlers.keys()], CHANNELS);
  assert.equal(controller.install(), false);
  assert.equal(controller.dispose(), true);
  assert.equal(handlers.size, 0);
  assert.equal(controller.dispose(), false);
});

test("lists only for the main renderer and projects unavailable state", () => {
  const { controller, setLauncher } = harness();
  assert.deepEqual(controller.list(mainEvent), { state: "ready", entries: [] });
  setLauncher(null);
  assert.deepEqual(controller.list(mainEvent), { state: "unavailable", entries: [] });
  assert.throws(() => controller.list({ sender: { id: 9 } }), /ipc_main_sender_rejected/);
});

test("program registration owns the native selection and never accepts a renderer path", async () => {
  const { controller, dialog, launcher, mainWindow } = harness();
  await assert.rejects(() => controller.addProgram(mainEvent, { name: "工作程序", path: "C:\\Injected.exe" }), /payload_rejected/);
  const options = [];
  dialog.showOpenDialog = async (window, value) => {
    assert.equal(window, mainWindow);
    options.push(value);
    return { canceled: false, filePaths: ["C:\\Program Files\\Tool\\tool.exe"] };
  };
  const payloads = [];
  launcher.addProgram = (payload) => { payloads.push(payload); return { id: "program:1" }; };
  assert.deepEqual(await controller.addProgram(mainEvent, { name: "工作程序" }), { status: "created", entry: { id: "program:1" } });
  assert.deepEqual(payloads, [{ name: "工作程序", selectedPath: "C:\\Program Files\\Tool\\tool.exe" }]);
  assert.deepEqual(options[0].properties, ["openFile"]);
  assert.deepEqual(options[0].filters[0].extensions, ["exe", "com"]);
});

test("program selection cancellation performs no mutation", async () => {
  const { controller, launcher } = harness();
  launcher.addProgram = () => assert.fail("cancelled selection must not register a program");
  assert.deepEqual(await controller.addProgram(mainEvent, { name: "工作程序" }), { status: "cancelled" });
});

test("bookmark, rename and delete keep strict payload contracts", () => {
  const { controller } = harness();
  assert.deepEqual(controller.addBookmark(mainEvent, { name: "站点", url: "https://example.com/" }), {
    status: "created", entry: { id: "bookmark:1", name: "站点", url: "https://example.com/" },
  });
  assert.deepEqual(controller.rename(mainEvent, { id: "bookmark:1", name: "新名称" }), {
    status: "renamed", entry: { id: "bookmark:1", name: "新名称" },
  });
  assert.deepEqual(controller.remove(mainEvent, { id: "bookmark:1" }), { status: "deleted", id: "bookmark:1" });
  assert.throws(() => controller.addBookmark(mainEvent, { name: "站点", url: "https://example.com/", path: "x" }), /payload_rejected/);
  assert.throws(() => controller.rename(mainEvent, { id: "bookmark:1" }), /payload_rejected/);
  assert.throws(() => controller.remove(mainEvent, { id: "bookmark:1", extra: "x" }), /payload_rejected/);
});

test("launch emits feedback only after the controller resolves", async () => {
  const { calls, controller, launcher } = harness();
  assert.deepEqual(await controller.launch(mainEvent, { id: "program:1" }), {
    status: "launched", id: "program:1", name: "工作程序",
  });
  assert.deepEqual(calls.slice(-2), [["authorize", 7], ["feedback", "program:1"]]);
  launcher.launch = async () => { throw new Error("launch_failed"); };
  await assert.rejects(() => controller.launch(mainEvent, { id: "program:1" }), /launch_failed/);
  assert.equal(calls.filter(([kind]) => kind === "feedback").length, 1);
});
