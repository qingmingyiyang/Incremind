const assert = require("node:assert/strict");
const test = require("node:test");

const {
  CHANNELS,
  SessionPlacementTransferIpcController,
} = require("../src/session-placement-transfer-ipc-controller.cjs");

const BUNDLE = Object.freeze({
  schema_version: "resume_bundle.v3",
  bundle_id: "bundle-1",
  source_device_id: "source-1",
  target_device_id: "target-1",
  project_id: "project-a",
  session_ref: "session-1",
  turn_refs: ["turn-1"],
  context_manifest_ref: "context-1",
  context_manifest_revision: "1",
  last_event_cursor: "3",
  display_summary: "只读会话",
  workspace_base_manifest_ref: "workspace:none",
  workspace_manifest: [],
  capability_descriptors: [],
  expires_at: "2030-01-01T00:00:00Z",
  source_trust_revision: 2,
  target_trust_revision: 3,
  nonce: "nonce-1",
  signature: "signature-1",
});

function response({ status = 200, body = BUNDLE } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return body; },
  };
}

function harness({
  visible = true,
  focused = true,
  session = { origin: "http://127.0.0.1:11495", secret: "desktop-secret" },
  dialogResults = [],
  responses = [],
  sourceText = JSON.stringify(BUNDLE),
  sourceStat = { size: 256, isFile: () => true, isSymbolicLink: () => false },
} = {}) {
  const handlers = new Map();
  const removed = [];
  const fetches = [];
  const writes = [];
  const reads = [];
  const guards = [];
  const dialogCalls = [];
  const mainWindow = {
    isDestroyed: () => false,
    isVisible: () => visible,
    isFocused: () => focused,
  };
  const controller = new SessionPlacementTransferIpcController({
    ipcMain: {
      handle(channel, handler) { handlers.set(channel, handler); },
      removeHandler(channel) { removed.push(channel); handlers.delete(channel); },
    },
    dialog: {
      async showSaveDialog(_window, options) {
        dialogCalls.push({ kind: "save", options });
        return dialogResults.shift() || { canceled: false, filePath: "C:\\exports\\session.json" };
      },
      async showOpenDialog(_window, options) {
        dialogCalls.push({ kind: "open", options });
        return dialogResults.shift() || { canceled: false, filePaths: ["C:\\imports\\session.json"] };
      },
    },
    requireMainRenderer(event) {
      guards.push(event);
      if (!event?.trusted) throw new Error("ipc_main_sender_rejected");
    },
    mainWindowProvider: () => mainWindow,
    sessionProvider: () => session,
    fetchImpl: async (url, options) => {
      fetches.push({ url, options });
      return responses.shift() || response();
    },
    fsPromises: {
      async writeFile(filePath, bytes, options) { writes.push({ filePath, bytes, options }); },
      async lstat(filePath) { reads.push({ kind: "lstat", filePath }); return sourceStat; },
      async readFile(filePath) { reads.push({ kind: "readFile", filePath }); return sourceText; },
    },
  });
  controller.install();
  return { controller, dialogCalls, fetches, guards, handlers, reads, removed, writes };
}

const trustedEvent = Object.freeze({ trusted: true });
const exportRequest = Object.freeze({
  project_id: "project-a",
  session_id: "session-1",
  target_device_id: "target-1",
  expires_at: "2030-01-01T00:00:00Z",
});

test("session transfer owns three main-only channels", () => {
  assert.deepEqual([...harness().handlers.keys()].sort(), [...CHANNELS].sort());
});

test("export selects a workspace before a target, sends its path only to loopback, and never returns raw bundle", async () => {
  const value = harness();
  const result = await value.handlers.get("chriptmas:session-placement-save")(trustedEvent, exportRequest);

  assert.deepEqual(result, { status: "saved", bundle_id: "bundle-1" });
  assert.equal(value.fetches.length, 1);
  assert.match(value.fetches[0].url, /\/api\/rebuild\/session-placement\/exports$/);
  assert.equal(value.fetches[0].options.headers["X-Chriptmas-Desktop-Session"], "desktop-secret");
  assert.deepEqual(JSON.parse(value.fetches[0].options.body), {
    ...exportRequest, workspace_path: "C:\\imports\\session.json",
  });
  assert.deepEqual(value.dialogCalls.map((item) => item.options.properties), [
    ["openDirectory", "dontAddToRecent"], ["createDirectory", "showOverwriteConfirmation"],
  ]);
  assert.equal(value.writes.length, 1);
  assert.deepEqual(JSON.parse(value.writes[0].bytes.toString("utf8")), BUNDLE);
  assert.equal(value.writes[0].options.flag, "wx");
  assert.equal(JSON.stringify(result).includes("signature-1"), false);
  assert.equal(JSON.stringify(result).includes("workspace_path"), false);

  const cancelled = harness({ dialogResults: [{ canceled: true }] });
  assert.deepEqual(
    await cancelled.handlers.get("chriptmas:session-placement-save")(trustedEvent, exportRequest),
    { status: "cancelled" },
  );
  assert.equal(cancelled.fetches.length, 0);
});

test("import accepts exact v2 bundles while v3 requires both trust revisions", async () => {
  const legacy = { ...BUNDLE, schema_version: "resume_bundle.v2" };
  delete legacy.source_trust_revision;
  delete legacy.target_trust_revision;
  const value = harness({
    sourceText: JSON.stringify(legacy),
    responses: [response({ body: { bundle_id: "bundle-1", read_only: true } })],
  });
  assert.deepEqual(
    await value.handlers.get("chriptmas:session-placement-import")(trustedEvent),
    { status: "imported", bundle_id: "bundle-1", read_only: true },
  );
  const missingTrust = { ...BUNDLE };
  delete missingTrust.source_trust_revision;
  const rejected = harness({ sourceText: JSON.stringify(missingTrust) });
  await assert.rejects(rejected.handlers.get("chriptmas:session-placement-import")(trustedEvent), /session_bundle_invalid/);
});

test("reconcile keeps workspace paths and manifests in main, returning only bounded counts", async () => {
  const value = harness({
    responses: [response({ body: {
      mode: "plan_only",
      entries: [{ relative_path: "private.txt" }],
      classifications: { unchanged: [], add: ["private.txt"], modify: [], delete: [], conflict: [] },
      has_conflicts: false,
      apply_supported: false,
      apply_boundary: "host-owned",
    } })],
  });
  const result = await value.handlers.get("chriptmas:session-placement-reconcile")(trustedEvent, {
    bundle_id: "bundle-1", project_id: "project-a",
  });

  assert.deepEqual(result, {
    status: "reconciled",
    counts: { unchanged: 0, add: 1, modify: 0, delete: 0, conflict: 0 },
    has_conflicts: false,
    apply_supported: false,
  });
  assert.deepEqual(JSON.parse(value.fetches[0].options.body), {
    project_id: "project-a", workspace_path: "C:\\imports\\session.json",
  });
  assert.match(value.fetches[0].url, /recoveries\/bundle-1\/reconcile-plan$/);
  assert.equal(JSON.stringify(result).includes("private.txt"), false);
  assert.equal(JSON.stringify(result).includes("workspace_path"), false);

  const invalid = harness();
  await assert.rejects(
    invalid.handlers.get("chriptmas:session-placement-reconcile")(trustedEvent, { bundle_id: "bundle-1", project_id: "project-a", raw: true }),
    /session_bundle_reconcile_invalid/,
  );
  assert.equal(invalid.fetches.length, 0);
});

test("export rejects untrusted renderers and non-exact DTOs before side effects", async () => {
  const value = harness();
  await assert.rejects(
    value.handlers.get("chriptmas:session-placement-save")({ trusted: false }, exportRequest),
    /ipc_main_sender_rejected/,
  );
  await assert.rejects(
    value.handlers.get("chriptmas:session-placement-save")(trustedEvent, { ...exportRequest, bundle: BUNDLE }),
    /session_bundle_request_invalid/,
  );
  assert.equal(value.fetches.length, 0);
  assert.equal(value.writes.length, 0);
});

test("import validates the local file in main and returns only a read-only reference", async () => {
  const value = harness({
    responses: [response({ body: { bundle_id: "bundle-1", read_only: true } })],
  });
  const result = await value.handlers.get("chriptmas:session-placement-import")(trustedEvent);

  assert.deepEqual(result, { status: "imported", bundle_id: "bundle-1", read_only: true });
  assert.deepEqual(value.reads.map((item) => item.kind), ["lstat", "readFile"]);
  assert.deepEqual(JSON.parse(value.fetches[0].options.body), { bundle: BUNDLE });
  assert.equal(JSON.stringify(result).includes("signature-1"), false);

  const linked = harness({
    sourceStat: { size: 256, isFile: () => true, isSymbolicLink: () => true },
  });
  await assert.rejects(
    linked.handlers.get("chriptmas:session-placement-import")(trustedEvent),
    /session_bundle_invalid/,
  );
  assert.equal(linked.fetches.length, 0);
});

test("window sidecar and response validation fail closed", async () => {
  const hidden = harness({ visible: false });
  await assert.rejects(
    hidden.handlers.get("chriptmas:session-placement-save")(trustedEvent, exportRequest),
    /session_bundle_window_required/,
  );
  const invalidOrigin = harness({ session: { origin: "https://example.com", secret: "x" } });
  await assert.rejects(
    invalidOrigin.handlers.get("chriptmas:session-placement-save")(trustedEvent, exportRequest),
    /session_bundle_sidecar_unavailable/,
  );
  const malformed = harness({ responses: [response({ body: { bundle_id: "bundle-1" } })] });
  await assert.rejects(
    malformed.handlers.get("chriptmas:session-placement-save")(trustedEvent, exportRequest),
    /session_bundle_invalid/,
  );
});

test("dispose removes only owned handlers", () => {
  const value = harness();
  assert.equal(value.controller.dispose(), true);
  assert.equal(value.controller.dispose(), false);
  assert.deepEqual(value.removed.sort(), [...CHANNELS].sort());
});
