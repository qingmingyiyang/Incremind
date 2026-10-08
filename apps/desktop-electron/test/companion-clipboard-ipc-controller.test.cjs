const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  CHANNELS,
  CompanionClipboardIpcController,
  SETTINGS_FILE,
} = require("../src/companion/clipboard-ipc-controller.cjs");

function fixture(t, overrides = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-clipboard-ipc-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const handlers = new Map();
  const removed = [];
  const calls = [];
  const watcher = {
    status: () => Object.freeze({ enabled: false, state: "disabled" }),
    setEnabled: (enabled) => {
      calls.push(["enabled", enabled]);
      return Object.freeze({ enabled, state: enabled ? "ready" : "disabled" });
    },
    stop: () => calls.push(["stop"]),
  };
  const overlayController = {
    current: null,
    close: () => calls.push(["close"]),
  };
  const options = {
    ipcMain: {
      handle: (channel, handler) => handlers.set(channel, handler),
      removeHandler: (channel) => { removed.push(channel); handlers.delete(channel); },
    },
    requireMainRenderer: (event) => {
      calls.push(["guard", event?.sender]);
      if (event?.sender !== "main") throw new Error("ipc_window_sender_rejected");
    },
    watcher,
    overlayController,
    userDataPathProvider: () => root,
    processId: 4242,
    ...overrides,
  };
  return { controller: new CompanionClipboardIpcController(options), handlers, removed, calls, watcher, overlayController, root };
}

test("installs and disposes the exact clipboard IPC surface and watcher lifecycle", (t) => {
  const value = fixture(t);
  assert.equal(value.controller.install(), true);
  assert.deepEqual([...value.handlers.keys()], CHANNELS);
  assert.equal(value.controller.install(), false);
  assert.equal(value.controller.dispose(), true);
  assert.deepEqual(value.removed, CHANNELS);
  assert.deepEqual(value.calls, [["stop"]]);
  assert.equal(value.controller.dispose(), false);
  assert.deepEqual(value.calls, [["stop"]]);
});

test("status validates the main renderer before reading the watcher", (t) => {
  let reads = 0;
  const value = fixture(t, {
    watcher: {
      status: () => { reads += 1; return { enabled: false }; },
      setEnabled: () => ({}),
      stop: () => {},
    },
  });
  value.controller.install();
  assert.throws(() => value.handlers.get(CHANNELS[0])({ sender: "pet" }), /ipc_window_sender_rejected/);
  assert.equal(reads, 0);
  assert.deepEqual(value.handlers.get(CHANNELS[0])({ sender: "main" }), { enabled: false });
  assert.equal(reads, 1);
});

test("initialization is default-off and restores only the fixed schema-v1 setting", (t) => {
  const value = fixture(t);
  assert.deepEqual(value.controller.initialize(), { enabled: false, state: "disabled" });
  fs.writeFileSync(path.join(value.root, SETTINGS_FILE), JSON.stringify({ schema_version: 2, enabled: true }), "utf8");
  value.controller.initialize();
  fs.writeFileSync(path.join(value.root, SETTINGS_FILE), JSON.stringify({ schema_version: 1, enabled: true }), "utf8");
  value.controller.initialize();
  assert.deepEqual(value.calls, [["enabled", false], ["enabled", false], ["enabled", true]]);
});

test("configuration persists before mutation and closes only a clipboard overlay on disable", (t) => {
  const value = fixture(t);
  value.controller.install();
  const configure = value.handlers.get(CHANNELS[1]);
  assert.deepEqual(configure({ sender: "main" }, { enabled: true }), { enabled: true, state: "ready" });
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(value.root, SETTINGS_FILE), "utf8")), { schema_version: 1, enabled: true });
  value.overlayController.current = { kind: "reminder_due" };
  configure({ sender: "main" }, { enabled: false });
  assert.equal(value.calls.some(([name]) => name === "close"), false);
  value.overlayController.current = { kind: "clipboard_comment" };
  configure({ sender: "main" }, { enabled: false });
  assert.equal(value.calls.filter(([name]) => name === "close").length, 1);
});

test("configuration rejects non-main senders and every non-boolean payload before persistence", (t) => {
  const value = fixture(t);
  value.controller.install();
  const configure = value.handlers.get(CHANNELS[1]);
  assert.throws(() => configure({ sender: "pet" }, { enabled: true }), /ipc_window_sender_rejected/);
  for (const payload of [null, [], {}, { enabled: 1 }, { enabled: true, path: "secret" }]) {
    assert.throws(() => configure({ sender: "main" }, payload), /companion_clipboard_setting_rejected/);
  }
  assert.equal(fs.existsSync(path.join(value.root, SETTINGS_FILE)), false);
  assert.equal(value.calls.some(([name]) => name === "enabled"), false);
});

test("persistence failure leaves watcher state unchanged and removes its temporary file", (t) => {
  const temporaryPaths = [];
  const fsImpl = {
    ...fs,
    writeFileSync: (target, contents, options) => {
      temporaryPaths.push(target);
      fs.writeFileSync(target, contents, options);
    },
    renameSync: () => { throw new Error("disk_full"); },
  };
  const value = fixture(t, { fsImpl });
  value.controller.install();
  assert.throws(() => value.handlers.get(CHANNELS[1])({ sender: "main" }, { enabled: true }), /disk_full/);
  assert.equal(value.calls.some(([name]) => name === "enabled"), false);
  assert.equal(temporaryPaths.length, 1);
  assert.equal(fs.existsSync(temporaryPaths[0]), false);
});

test("failed installation rolls back only clipboard handlers already registered", (t) => {
  let count = 0;
  const removed = [];
  const value = fixture(t, {
    ipcMain: {
      handle: () => { count += 1; if (count === 2) throw new Error("registration_failed"); },
      removeHandler: (channel) => removed.push(channel),
    },
  });
  assert.throws(() => value.controller.install(), /registration_failed/);
  assert.deepEqual(removed, [CHANNELS[0]]);
  assert.equal(value.controller.installed, false);
});
