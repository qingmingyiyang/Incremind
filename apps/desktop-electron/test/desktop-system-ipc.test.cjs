const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const { DesktopSystemIpcController } = require("../src/desktop-system-ipc.cjs");

function harness({ notificationSupported = true } = {}) {
  const handlers = new Map();
  const removed = [];
  const shortcuts = [];
  const unregistered = [];
  const notifications = [];
  const opened = [];
  const openedPaths = [];
  const appearances = [];
  const guarded = [];

  class FakeNotification {
    static isSupported() {
      return notificationSupported;
    }

    constructor(options) {
      this.options = options;
    }

    show() {
      notifications.push(this.options);
    }
  }

  const controller = new DesktopSystemIpcController({
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
    app: { getPath: (name) => name === "userData" ? "C:\\Temp\\Chriptmas" : "", getVersion: () => "0.0.0-test" },
    Notification: FakeNotification,
    globalShortcut: {
      unregister(accelerator) {
        unregistered.push(accelerator);
      },
      register(accelerator, callback) {
        shortcuts.push({ accelerator, callback });
        return accelerator !== "Ctrl+Alt+Fail";
      },
      unregisterAll() {
        unregistered.push("*");
      },
    },
    shell: {
      async openPath(targetPath) {
        openedPaths.push(targetPath);
        return targetPath.includes("missing") ? "path not found" : "";
      },
    },
    requireMainRenderer(event) {
      guarded.push(event);
      if (event?.trusted !== true) throw new Error("ipc_main_sender_rejected");
    },
    showMainWindow() {
      opened.push("main");
    },
    setWindowAppearance(theme) {
      appearances.push(theme);
    },
    platform: "win32",
    pathSeparator: "\\",
    candidateIdentityProvider: () => Object.freeze({ status: "not_run", reason: "test" }),
  });

  controller.install();
  return { appearances, controller, guarded, handlers, notifications, opened, openedPaths, removed, shortcuts, unregistered };
}

const event = Object.freeze({ trusted: true });

test("desktop system controller owns the exact privileged IPC surface", () => {
  const { handlers } = harness();
  assert.deepEqual([...handlers.keys()].sort(), [
    "chriptmas:auto-update-status",
    "chriptmas:open-path",
    "chriptmas:platform-info",
    "chriptmas:register-shortcut",
    "chriptmas:show-notification",
    "chriptmas:window-appearance",
  ]);
});

test("every desktop system handler rejects a non-main renderer before work", async () => {
  const { guarded, handlers, notifications, shortcuts } = harness();
  for (const [channel, handler] of handlers) {
    await assert.rejects(async () => handler({ trusted: false }, {}), /ipc_main_sender_rejected/, channel);
  }
  assert.equal(guarded.length, handlers.size);
  assert.deepEqual(notifications, []);
  assert.deepEqual(shortcuts, []);
});

test("platform and update projections preserve the renderer contract", async () => {
  const { handlers } = harness();
  assert.deepEqual(await handlers.get("chriptmas:platform-info")(event), {
    appDataDir: "C:\\Temp\\Chriptmas",
    pathSeparator: "\\",
    platform: "win32",
    shell: "electron",
    version: "0.0.0-test",
    candidate: { status: "not_run", reason: "test" },
  });
  assert.deepEqual(await handlers.get("chriptmas:auto-update-status")(event), {
    status: "unavailable",
    reason: "auto-update is not configured",
    enabled: false,
  });
});

test("path opening validates the main renderer and preserves Shell failure projection", async () => {
  const value = harness();
  const handler = value.handlers.get("chriptmas:open-path");
  assert.deepEqual(await handler(event, { path: "  C:\\Exports  " }), { status: "opened" });
  assert.deepEqual(value.openedPaths, ["C:\\Exports"]);
  assert.deepEqual(await handler(event, { path: "C:\\missing" }), { status: "failed", reason: "path not found" });
  assert.deepEqual(await handler(event, { path: "\0" }), { status: "rejected", reason: "path is required" });
  assert.equal(value.openedPaths.length, 2);
});

test("notification text stays bounded and unsupported hosts fail closed", async () => {
  const supported = harness();
  assert.deepEqual(await supported.handlers.get("chriptmas:show-notification")(event, {
    title: `  ${"题".repeat(200)}  `,
    body: "第一行\n  第二行",
  }), { status: "shown" });
  assert.equal(supported.notifications[0].title.length, 180);
  assert.equal(supported.notifications[0].body, "第一行 第二行");

  const unsupported = harness({ notificationSupported: false });
  assert.deepEqual(await unsupported.handlers.get("chriptmas:show-notification")(event, {}), { status: "unsupported" });
  assert.deepEqual(unsupported.notifications, []);
});

test("shortcut registration validates input and keeps the main-window callback", async () => {
  const value = harness();
  const handler = value.handlers.get("chriptmas:register-shortcut");
  assert.deepEqual(await handler(event, { accelerator: "\0" }), {
    status: "rejected",
    reason: "invalid accelerator",
  });
  assert.deepEqual(await handler(event, { accelerator: " Ctrl+Shift+M " }), { status: "registered" });
  assert.deepEqual(value.unregistered, ["Ctrl+Shift+M"]);
  assert.equal(value.shortcuts[0].accelerator, "Ctrl+Shift+M");
  value.shortcuts[0].callback();
  assert.deepEqual(value.opened, ["main"]);
  assert.deepEqual(await handler(event, { accelerator: "Ctrl+Alt+Fail" }), { status: "failed" });
});

test("window appearance accepts only the three renderer theme modes", async () => {
  const value = harness();
  const handler = value.handlers.get("chriptmas:window-appearance");
  assert.deepEqual(await handler(event, { theme: "dark" }), { status: "applied", theme: "dark" });
  assert.deepEqual(await handler(event, { theme: "system" }), { status: "applied", theme: "system" });
  assert.deepEqual(await handler(event, { theme: "neon" }), { status: "rejected", reason: "invalid theme" });
  assert.deepEqual(value.appearances, ["dark", "system"]);
});

test("main preload exposes the bounded window appearance bridge", () => {
  const preload = fs.readFileSync(path.join(__dirname, "..", "src", "preload.cjs"), "utf8");
  assert.match(preload, /setWindowAppearance: \(theme\)/);
  assert.match(preload, /ipcRenderer\.invoke\("chriptmas:window-appearance"/);
  assert.match(preload, /\["light", "dark", "system"\]\.includes\(theme\)/);
});

test("main composes native theme ownership into the window and guarded IPC paths", () => {
  const main = fs.readFileSync(path.join(__dirname, "..", "src", "main.cjs"), "utf8");
  assert.match(main, /configureMainWindow: \(window\) => configureMainWindowPresentation\(window, \{ nativeTheme \}\)/);
  assert.match(main, /setWindowAppearance: \(theme\) => \{[\s\S]*?nativeTheme\.themeSource = theme/);
  assert.match(main, /resolveMainWindowTitleBar\(nativeTheme\.shouldUseDarkColors === true\)/);
});

test("dispose removes owned handlers and releases shortcuts exactly once", () => {
  const value = harness();
  assert.equal(value.controller.dispose(), true);
  assert.equal(value.controller.dispose(), false);
  assert.deepEqual(value.handlers.size, 0);
  assert.deepEqual(value.removed.sort(), [
    "chriptmas:auto-update-status",
    "chriptmas:open-path",
    "chriptmas:platform-info",
    "chriptmas:register-shortcut",
    "chriptmas:show-notification",
    "chriptmas:window-appearance",
  ]);
  assert.deepEqual(value.unregistered, ["*"]);
});
