const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");

const {
  CHANNELS,
  CompanionDataIpcController,
  RESTORE_GRANT_TTL_MS,
} = require("../src/companion/data-ipc-controller.cjs");

function harness() {
  const handlers = new Map();
  const fetches = [];
  const shellPaths = [];
  const mainWindow = { isVisible: () => true, isFocused: () => true };
  let activeWindow = mainWindow;
  let activeSession = { origin: "http://127.0.0.1:11495", secret: "session-secret" };
  let currentTime = Date.parse("2026-08-15T08:00:00Z");
  let saveSelection = { canceled: true, filePath: undefined };
  let openSelection = { canceled: true, filePaths: [] };
  let confirmation = { response: 0 };
  let responsePayload = null;
  const dialog = {
    showSaveDialog: async () => saveSelection,
    showOpenDialog: async () => openSelection,
    showMessageBox: async () => confirmation,
  };
  const controller = new CompanionDataIpcController({
    ipcMain: {
      handle: (channel, handler) => handlers.set(channel, handler),
      removeHandler: (channel) => handlers.delete(channel),
    },
    dialog,
    shell: { openPath: async (value) => { shellPaths.push(value); return ""; } },
    crypto,
    fetchImpl: async (url, options) => {
      fetches.push({ url, options });
      const payload = responsePayload || (url.endsWith("/restore/preflight")
        ? { status: "ready", fingerprint: "sha256:backup" }
        : { status: url.endsWith("/restore") ? "restored" : "backed_up" });
      return { ok: true, status: 200, json: async () => payload };
    },
    sessionHeader: "X-Chriptmas-Session",
    sessionProvider: () => activeSession,
    mainWindowProvider: () => activeWindow,
    manualPathProvider: () => "C:\\AppData\\companion\\readme.md",
    requireMainRenderer: (event) => {
      if (![1, 2].includes(event.sender.id)) throw new Error("ipc_main_sender_rejected");
    },
    createTimeoutSignal: (milliseconds) => ({ milliseconds }),
    now: () => currentTime,
  });
  return {
    controller,
    dialog,
    fetches,
    handlers,
    mainWindow,
    shellPaths,
    setConfirmation: (value) => { confirmation = value; },
    setNow: (value) => { currentTime = value; },
    setOpenSelection: (value) => { openSelection = value; },
    setResponsePayload: (value) => { responsePayload = value; },
    setSaveSelection: (value) => { saveSelection = value; },
    setSession: (value) => { activeSession = value; },
    setWindow: (value) => { activeWindow = value; },
  };
}

const mainEvent = { sender: { id: 1 } };

test("installs and disposes the fixed companion data IPC allowlist", () => {
  const { controller, handlers } = harness();
  assert.equal(controller.install(), true);
  assert.deepEqual([...handlers.keys()], CHANNELS);
  assert.equal(controller.install(), false);
  assert.equal(controller.dispose(), true);
  assert.equal(handlers.size, 0);
  assert.equal(controller.dispose(), false);
});

test("opens only the fixed main-owned manual authority", async () => {
  const { controller, shellPaths } = harness();
  assert.deepEqual(await controller.openManual(mainEvent), { status: "opened" });
  assert.deepEqual(shellPaths, ["C:\\AppData\\companion\\readme.md"]);
  await assert.rejects(() => controller.openManual({ sender: { id: 99 } }), /ipc_main_sender_rejected/);
});

test("backup selects the path natively and signs the exact sidecar request", async () => {
  const { controller, fetches, setSaveSelection } = harness();
  assert.deepEqual(await controller.backup(mainEvent), { status: "cancelled" });
  assert.equal(fetches.length, 0);
  setSaveSelection({ canceled: false, filePath: "C:\\Backups\\companion.sqlite3" });

  assert.deepEqual(await controller.backup(mainEvent), { status: "backed_up" });
  assert.equal(fetches.length, 1);
  const call = fetches[0];
  assert.equal(call.url, "http://127.0.0.1:11495/api/rebuild/companion/data/backup");
  assert.deepEqual(JSON.parse(call.options.body), { path: "C:\\Backups\\companion.sqlite3" });
  assert.equal(call.options.signal.milliseconds, 30000);
  assert.equal(call.options.headers["X-Chriptmas-Session"], "session-secret");
  const expectedSignature = crypto.createHmac("sha256", "session-secret")
    .update("companion-data:backup:C:\\Backups\\companion.sqlite3:")
    .digest("hex");
  assert.equal(call.options.headers["X-Chriptmas-Main-Signature"], expectedSignature);
});

test("restore uses a sender fingerprint and TTL-bound grant plus native confirmation", async () => {
  const {
    controller, fetches, setConfirmation, setOpenSelection,
  } = harness();
  setOpenSelection({ canceled: false, filePaths: ["C:\\Backups\\restore.sqlite3"] });
  const preflight = await controller.restorePreflight(mainEvent);
  assert.equal(preflight.fingerprint, "sha256:backup");
  assert.equal(typeof preflight.grant_id, "string");
  assert.equal(Object.hasOwn(preflight, "path"), false);

  const grant = { grant_id: preflight.grant_id, expected_fingerprint: preflight.fingerprint };
  assert.deepEqual(await controller.restore(mainEvent, grant), { status: "cancelled" });
  assert.equal(fetches.length, 1, "cancelled confirmation must not call restore");

  setConfirmation({ response: 1 });
  assert.deepEqual(await controller.restore(mainEvent, grant), { status: "restored" });
  assert.equal(fetches.length, 2);
  assert.deepEqual(JSON.parse(fetches[1].options.body), {
    path: "C:\\Backups\\restore.sqlite3",
    expected_fingerprint: "sha256:backup",
  });
  await assert.rejects(() => controller.restore(mainEvent, grant), /grant_rejected/);
});

test("restore grants fail closed for another sender mismatch expiry and malformed preflight", async () => {
  const {
    controller, setNow, setOpenSelection, setResponsePayload,
  } = harness();
  const startedAt = Date.parse("2026-08-15T08:00:00Z");
  setOpenSelection({ canceled: false, filePaths: ["C:\\Backups\\restore.sqlite3"] });

  let preflight = await controller.restorePreflight(mainEvent);
  await assert.rejects(
    () => controller.restore({ sender: { id: 2 } }, {
      grant_id: preflight.grant_id, expected_fingerprint: preflight.fingerprint,
    }),
    /grant_rejected/,
  );

  preflight = await controller.restorePreflight(mainEvent);
  await assert.rejects(
    () => controller.restore(mainEvent, {
      grant_id: preflight.grant_id, expected_fingerprint: "sha256:other",
    }),
    /grant_rejected/,
  );

  preflight = await controller.restorePreflight(mainEvent);
  setNow(startedAt + RESTORE_GRANT_TTL_MS);
  await assert.rejects(
    () => controller.restore(mainEvent, {
      grant_id: preflight.grant_id, expected_fingerprint: preflight.fingerprint,
    }),
    /grant_rejected/,
  );

  setNow(startedAt);
  setResponsePayload({ status: "ready" });
  await assert.rejects(() => controller.restorePreflight(mainEvent), /preflight_invalid/);
});

test("all mutations require a visible focused main window and an active sidecar", async () => {
  const {
    controller, fetches, setSaveSelection, setSession, setWindow,
  } = harness();
  setWindow({ isVisible: () => true, isFocused: () => false });
  await assert.rejects(() => controller.backup(mainEvent), /window_required/);
  assert.equal(fetches.length, 0);

  setWindow({ isVisible: () => true, isFocused: () => true });
  setSaveSelection({ canceled: false, filePath: "C:\\Backups\\companion.sqlite3" });
  setSession(null);
  await assert.rejects(() => controller.backup(mainEvent), /sidecar_unavailable/);
});
