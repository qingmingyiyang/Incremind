const assert = require("node:assert/strict");
const test = require("node:test");

const { MemoryTransferIpcController } = require("../src/memory-transfer-ipc-controller.cjs");

function response({ status = 200, body = {}, bytes, headers = {} } = {}) {
  const buffer = bytes || Buffer.from(JSON.stringify(body), "utf8");
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name) => headers[name.toLowerCase()] ?? null },
    async arrayBuffer() { return Uint8Array.from(buffer).buffer; },
    async json() { return body; },
  };
}

function harness({
  visible = true,
  focused = true,
  session = { origin: "http://127.0.0.1:11495", secret: "secret", instance_id: "instance-1" },
  dialogResults = [],
  responses = [],
  sourceStat = { size: 4, isFile: () => true, isSymbolicLink: () => false },
  sourceBytes = Buffer.from("PK\u0003\u0004"),
  afterRead = null,
} = {}) {
  let activeSession = session;
  const handlers = new Map();
  const removed = [];
  const guards = [];
  const dialogs = [];
  const fetches = [];
  const assetWrites = [];
  const presetWrites = [];
  const reads = [];
  const mainWindow = {
    isDestroyed: () => false,
    isVisible: () => visible,
    isFocused: () => focused,
  };
  const controller = new MemoryTransferIpcController({
    ipcMain: {
      handle(channel, handler) { handlers.set(channel, handler); },
      removeHandler(channel) { removed.push(channel); handlers.delete(channel); },
    },
    dialog: {
      async showSaveDialog(window, options) { dialogs.push({ kind: "save", window, options }); return dialogResults.shift() || { canceled: false, filePath: "C:\\exports\\memory.json" }; },
      async showOpenDialog(window, options) { dialogs.push({ kind: "open", window, options }); return dialogResults.shift() || { canceled: false, filePaths: ["C:\\imports\\memory.zip"] }; },
    },
    requireMainRenderer(event) {
      guards.push(event);
      if (!event?.trusted) throw new Error("ipc_main_sender_rejected");
    },
    mainWindowProvider: () => mainWindow,
    sessionProvider: () => activeSession,
    fetchImpl: async (url, options) => { fetches.push({ url, options }); return responses.shift() || response(); },
    timeoutSignal: (milliseconds) => ({ timeout: milliseconds }),
    fsPromises: {
      async lstat(filePath) { reads.push({ kind: "lstat", filePath }); return sourceStat; },
      async readFile(filePath) { reads.push({ kind: "read", filePath }); if (afterRead) activeSession = afterRead(activeSession); return sourceBytes; },
    },
    pathApi: {
      extname: (filePath) => filePath.slice(filePath.lastIndexOf(".")),
      basename: (filePath) => filePath.slice(Math.max(filePath.lastIndexOf("\\"), filePath.lastIndexOf("/")) + 1),
    },
    writeMemoryAssetPackage: (value) => { assetWrites.push(value); return { status: "saved", path: value.targetPath }; },
    validateMemoryExportRequest: (payload) => {
      if (payload?.invalid) throw new Error("memory_export_payload_invalid");
      return { preset: "markdown", scope: { project_id: "project-1" }, config: { extension: ".md", label: "Markdown" } };
    },
    writeMemoryPresetExport: (value) => { presetWrites.push(value); return { status: "saved", path: value.targetPath }; },
    maxPackageBytes: 16,
    maxExportBytes: 16,
    now: () => new Date("2026-08-15T10:00:00.000Z"),
  });
  controller.install();
  return { assetWrites, controller, dialogs, fetches, guards, handlers, presetWrites, reads, removed };
}

const trustedEvent = Object.freeze({ trusted: true });

test("memory transfer controller owns exactly three main-only channels", () => {
  const value = harness();
  assert.deepEqual([...value.handlers.keys()].sort(), [
    "chriptmas:memory-assets-export",
    "chriptmas:memory-assets-import",
    "chriptmas:memory-export-save",
  ]);
});

test("all transfer handlers reject an unknown or non-visible renderer before side effects", async () => {
  const unknown = harness();
  for (const [channel, handler] of unknown.handlers) {
    await assert.rejects(async () => handler({ trusted: false }, {}), /ipc_main_sender_rejected/, channel);
  }
  assert.deepEqual(unknown.dialogs, []);
  assert.deepEqual(unknown.fetches, []);

  const hidden = harness({ visible: false });
  await assert.rejects(hidden.handlers.get("chriptmas:memory-assets-export")(trustedEvent), /memory_asset_export_window_required/);
  await assert.rejects(hidden.handlers.get("chriptmas:memory-assets-import")(trustedEvent), /memory_asset_import_window_required/);
  await assert.rejects(hidden.handlers.get("chriptmas:memory-export-save")(trustedEvent, {}), /memory_export_window_required/);
  assert.deepEqual(hidden.dialogs, []);
});

test("asset export selects a target before authenticated fetch and bounded write", async () => {
  const value = harness({ responses: [response({ body: { revision: 1 }, headers: { "content-length": "14" } })] });
  assert.deepEqual(await value.handlers.get("chriptmas:memory-assets-export")(trustedEvent), {
    status: "saved",
    path: "C:\\exports\\memory.json",
  });
  assert.equal(value.dialogs[0].kind, "save");
  assert.match(value.dialogs[0].options.defaultPath, /Chriptmas-memory-assets-2026-08-15\.json/);
  assert.match(value.fetches[0].url, /\/api\/rebuild\/memory-assets\/export$/);
  assert.equal(value.fetches[0].options.headers["X-Chriptmas-Desktop-Session"], "secret");
  assert.deepEqual(value.assetWrites[0], { targetPath: "C:\\exports\\memory.json", packagePayload: { revision: 1 } });

  const cancelled = harness({ dialogResults: [{ canceled: true }] });
  assert.deepEqual(await cancelled.handlers.get("chriptmas:memory-assets-export")(trustedEvent), { status: "cancelled" });
  assert.deepEqual(cancelled.fetches, []);
});

test("asset export rejects declared actual and malformed payloads", async () => {
  const declared = harness({ responses: [response({ headers: { "content-length": "17" } })] });
  await assert.rejects(declared.handlers.get("chriptmas:memory-assets-export")(trustedEvent), /memory_asset_export_too_large/);
  const actual = harness({ responses: [response({ bytes: Buffer.alloc(17), headers: { "content-length": "0" } })] });
  await assert.rejects(actual.handlers.get("chriptmas:memory-assets-export")(trustedEvent), /memory_asset_export_too_large/);
  const malformed = harness({ responses: [response({ bytes: Buffer.from("not-json") })] });
  await assert.rejects(malformed.handlers.get("chriptmas:memory-assets-export")(trustedEvent), /memory_asset_export_payload_invalid/);
});

test("asset import validates a single ordinary ZIP and sends only base64 bytes", async () => {
  const value = harness({ responses: [response({ body: { restored: 3 } })] });
  assert.deepEqual(await value.handlers.get("chriptmas:memory-assets-import")(trustedEvent), {
    status: "imported",
    file_name: "memory.zip",
    result: { restored: 3 },
  });
  assert.deepEqual(value.reads.map((item) => item.kind), ["lstat", "read"]);
  assert.match(value.fetches[0].url, /\/api\/rebuild\/memory\/export\/round-trip$/);
  assert.deepEqual(JSON.parse(value.fetches[0].options.body), { zip_base64: Buffer.from("PK\u0003\u0004").toString("base64") });
  assert.doesNotMatch(value.fetches[0].options.body, /C:\\\\imports/);

  const wrongType = harness({ dialogResults: [{ canceled: false, filePaths: ["C:\\imports\\memory.json"] }] });
  await assert.rejects(wrongType.handlers.get("chriptmas:memory-assets-import")(trustedEvent), /memory_asset_import_type_invalid/);
  const linked = harness({ sourceStat: { size: 4, isFile: () => true, isSymbolicLink: () => true } });
  await assert.rejects(linked.handlers.get("chriptmas:memory-assets-import")(trustedEvent), /memory_asset_import_source_invalid/);
});

test("asset import uses the renewed credential after reading and rejects a replacement instance", async () => {
  const rotated = harness({ afterRead: (session) => ({ ...session, secret: "renewed" }) });
  assert.equal((await rotated.handlers.get("chriptmas:memory-assets-import")(trustedEvent)).status, "imported");
  assert.equal(rotated.fetches[0].options.headers["X-Chriptmas-Desktop-Session"], "renewed");

  const replaced = harness({ afterRead: (session) => ({ ...session, instance_id: "instance-2", secret: "replacement" }) });
  await assert.rejects(replaced.handlers.get("chriptmas:memory-assets-import")(trustedEvent), /desktop_session_instance_changed/);
  assert.deepEqual(replaced.fetches, []);
});

test("transfer routes preserve sidecar unavailability HTTP errors and import size bounds", async () => {
  const noSession = harness({ session: null });
  await assert.rejects(noSession.handlers.get("chriptmas:memory-assets-export")(trustedEvent), /memory_asset_export_sidecar_unavailable/);
  assert.equal(noSession.dialogs.length, 1);
  assert.deepEqual(noSession.fetches, []);

  const exportHttp = harness({ responses: [response({ status: 503 })] });
  await assert.rejects(exportHttp.handlers.get("chriptmas:memory-assets-export")(trustedEvent), /memory_asset_export_http_503/);
  const importHttp = harness({ responses: [response({ status: 409, body: { reason: "fingerprint_mismatch" } })] });
  await assert.rejects(importHttp.handlers.get("chriptmas:memory-assets-import")(trustedEvent), /memory_asset_import_rejected:fingerprint_mismatch/);
  const presetHttp = harness({ responses: [response({ status: 502 })] });
  await assert.rejects(presetHttp.handlers.get("chriptmas:memory-export-save")(trustedEvent, {}), /memory_export_http_502/);

  const declaredTooLarge = harness({ sourceStat: { size: 17, isFile: () => true, isSymbolicLink: () => false } });
  await assert.rejects(declaredTooLarge.handlers.get("chriptmas:memory-assets-import")(trustedEvent), /memory_asset_import_size_invalid/);
  const changedTooLarge = harness({ sourceBytes: Buffer.alloc(17) });
  await assert.rejects(changedTooLarge.handlers.get("chriptmas:memory-assets-import")(trustedEvent), /memory_asset_import_size_changed/);
});

test("preset export validates before dialog then writes bounded response bytes", async () => {
  const value = harness({
    dialogResults: [{ canceled: false, filePath: "C:\\exports\\memory.md" }],
    responses: [response({ bytes: Buffer.from("# Memory"), headers: { "x-chriptmas-export-format": "markdown", "content-length": "8" } })],
  });
  assert.deepEqual(await value.handlers.get("chriptmas:memory-export-save")(trustedEvent, { preset: "markdown" }), {
    status: "saved",
    path: "C:\\exports\\memory.md",
  });
  assert.equal(value.dialogs[0].options.filters[0].extensions[0], "md");
  assert.match(value.fetches[0].url, /\/api\/rebuild\/memory\/export\/file$/);
  assert.deepEqual(value.presetWrites[0], {
    targetPath: "C:\\exports\\memory.md",
    bytes: Buffer.from("# Memory"),
    preset: "markdown",
    responseFormat: "markdown",
  });

  const invalid = harness();
  await assert.rejects(invalid.handlers.get("chriptmas:memory-export-save")(trustedEvent, { invalid: true }), /memory_export_payload_invalid/);
  assert.deepEqual(invalid.dialogs, []);
});

test("dispose removes owned handlers exactly once", () => {
  const value = harness();
  assert.equal(value.controller.dispose(), true);
  assert.equal(value.controller.dispose(), false);
  assert.equal(value.handlers.size, 0);
  assert.deepEqual(value.removed.sort(), [
    "chriptmas:memory-assets-export",
    "chriptmas:memory-assets-import",
    "chriptmas:memory-export-save",
  ]);
});
