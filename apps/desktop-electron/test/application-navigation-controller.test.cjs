const assert = require("node:assert/strict");
const test = require("node:test");

const {
  ApplicationNavigationController,
  MAIN_NAVIGATION_EVENT,
  MAIN_NAVIGATION_READY_EVENT,
} = require("../src/application-navigation-controller.cjs");

function fixture(overrides = {}) {
  const listeners = new Map();
  const removed = [];
  const sent = [];
  const shown = [];
  let loading = true;
  const mainFrame = { routingId: 9 };
  const webContents = {
    id: 42,
    mainFrame,
    isDestroyed: () => false,
    isLoadingMainFrame: () => loading,
    send: (channel, payload) => sent.push([channel, payload]),
  };
  const mainWindow = { isDestroyed: () => false, webContents };
  const options = {
    ipcMain: {
      on: (channel, listener) => listeners.set(channel, listener),
      removeListener: (channel, listener) => {
        removed.push([channel, listener]);
        if (listeners.get(channel) === listener) listeners.delete(channel);
      },
    },
    mainWindowProvider: () => mainWindow,
    showMainWindow: () => shown.push("main"),
    validatePanelPayload: ({ panel }) => {
      if (!["chat", "focus"].includes(panel)) throw new Error("invalid_panel");
      return Object.freeze({ panel });
    },
    ...overrides,
  };
  return {
    controller: new ApplicationNavigationController(options),
    listeners,
    removed,
    sent,
    shown,
    mainFrame,
    webContents,
    mainWindow,
    setLoading: (value) => { loading = value; },
  };
}

test("installs and disposes the fixed navigation-ready event", () => {
  const value = fixture();
  assert.equal(value.controller.install(), true);
  assert.deepEqual([...value.listeners.keys()], [MAIN_NAVIGATION_READY_EVENT]);
  assert.equal(value.controller.install(), false);
  assert.equal(value.controller.dispose(), true);
  assert.deepEqual(value.removed.map(([channel]) => channel), [MAIN_NAVIGATION_READY_EVENT]);
  assert.equal(value.controller.dispose(), false);
});

test("queues a bounded companion destination until the current main renderer is ready", () => {
  const value = fixture();
  value.controller.install();
  assert.deepEqual(value.controller.openCompanion("chat", "weekly_memory_review"), {
    status: "shown", panel: "chat", intent: "weekly_memory_review",
  });
  assert.deepEqual(value.shown, ["main"]);
  assert.deepEqual(value.sent, []);

  const ready = value.listeners.get(MAIN_NAVIGATION_READY_EVENT);
  assert.equal(ready({ sender: { id: 42 }, senderFrame: value.mainFrame }), false);
  assert.equal(ready({ sender: value.webContents, senderFrame: { routingId: 9 } }), false);
  assert.deepEqual(value.sent, []);

  value.setLoading(false);
  assert.equal(ready({ sender: value.webContents, senderFrame: value.mainFrame }), true);
  assert.deepEqual(value.sent, [[MAIN_NAVIGATION_EVENT, {
    view: "rebuild-companion", panel: "chat", intent: "weekly_memory_review",
  }]]);
  assert.equal(value.controller.deliver(), false);
});

test("drops unsupported intent and rejects an invalid companion panel", () => {
  const value = fixture();
  value.setLoading(false);
  assert.deepEqual(value.controller.openCompanion("focus", "untrusted"), {
    status: "shown", panel: "focus", intent: "",
  });
  assert.deepEqual(value.sent[0][1], { view: "rebuild-companion", panel: "focus" });
  assert.throws(() => value.controller.openCompanion("settings"), /invalid_panel/);
});

test("pending-memory navigation uses the fixed Library review destination", () => {
  const value = fixture();
  value.setLoading(false);
  assert.deepEqual(value.controller.openPendingMemoryReview(), {
    status: "shown", view: "rebuild-library-overview", filter: "pending_memory",
  });
  assert.deepEqual(value.sent, [[MAIN_NAVIGATION_EVENT, {
    view: "rebuild-library-overview", filter: "pending_memory",
  }]]);
  assert.equal(Object.isFrozen(value.sent[0][1]), true);
});

test("missing, destroyed and loading windows retain the latest pending destination", () => {
  let currentWindow = null;
  const value = fixture({ mainWindowProvider: () => currentWindow });
  value.controller.openCompanion("chat");
  assert.equal(value.controller.deliver(), false);
  currentWindow = { isDestroyed: () => true, webContents: value.webContents };
  assert.equal(value.controller.deliver(), false);
  currentWindow = value.mainWindow;
  assert.equal(value.controller.deliver(), false);
  value.setLoading(false);
  assert.equal(value.controller.deliver(), true);
  assert.deepEqual(value.sent[0][1], { view: "rebuild-companion", panel: "chat" });
});

test("a failed renderer send retains pending navigation for a later replay", () => {
  let fail = true;
  const value = fixture();
  value.setLoading(false);
  value.webContents.send = (channel, payload) => {
    if (fail) throw new Error("renderer_unavailable");
    value.sent.push([channel, payload]);
  };
  assert.throws(() => value.controller.openCompanion("chat"), /renderer_unavailable/);
  fail = false;
  assert.equal(value.controller.deliver(), true);
  assert.equal(value.sent.length, 1);
});

test("failed listener installation remains uninstalled", () => {
  const value = fixture({
    ipcMain: {
      on: () => { throw new Error("registration_failed"); },
      removeListener: () => { throw new Error("must_not_remove"); },
    },
  });
  assert.throws(() => value.controller.install(), /registration_failed/);
  assert.equal(value.controller.installed, false);
  assert.equal(value.controller.dispose(), false);
});
