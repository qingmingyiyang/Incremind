const assert = require("node:assert/strict");
const test = require("node:test");

const { CHANNEL, ContextGraphImportIpcController } = require("../src/context-graph-import-ipc-controller.cjs");

const trustedEvent = Object.freeze({ trusted: true, sender: Object.freeze({ id: 17 }) });
const request = Object.freeze({ projectId: "project-a", sourceType: "thoughtdag", commandId: "command-123" });

function response(payload, status = 201) {
  return { ok: status >= 200 && status < 300, status, async json() { return payload; } };
}

function harness({ session = { origin: "http://127.0.0.1:11495", secret: "desktop-secret", instance_id: "desktop-1" }, dialogResult, uploadResult, selectionResponse, fetchError, onUpload = () => {} } = {}) {
  const handlers = new Map();
  const calls = [];
  const controller = new ContextGraphImportIpcController({
    ipcMain: {
      handle(channel, handler) { assert.equal(handlers.has(channel), false); handlers.set(channel, handler); },
      removeHandler(channel) { handlers.delete(channel); },
    },
    dialog: {
      async showOpenDialog(window, options) {
        calls.push(["dialog", window, options]);
        return dialogResult || { canceled: false, filePaths: ["C:\\private\\strategy.thoughtdag.json"] };
      },
    },
    requireMainRenderer(event) {
      calls.push(["guard", event]);
      if (event?.trusted !== true) throw new Error("ipc_main_sender_rejected");
    },
    mainWindowProvider: () => "main-window",
    sessionProvider: () => { calls.push(["session"]); return session; },
    createFileGrant: async (options) => { calls.push(["grant", options]); return { grant_id: "grant-1" }; },
    grantHeaders: (grant, secret) => {
      calls.push(["grant-headers", grant, secret]);
      return {
        "Content-Length": "5",
        "X-Chriptmas-File-Grant": "file-grant-signed",
        "X-Chriptmas-File-Signature": "signed-proof",
      };
    },
    uploadFileGrant: async (grant, active, options) => {
      calls.push(["upload", grant, active, options]);
      onUpload(active);
      return uploadResult || { asset_id: "asset-123" };
    },
    fetchImpl: async (url, options) => {
      calls.push(["fetch", String(url), options]);
      if (fetchError) throw fetchError;
      return selectionResponse || response({
        selection_id: "selection-123",
        project_id: "project-a",
        source_type: "thoughtdag",
        display_name: "strategy.thoughtdag.json",
        expires_at: "2026-08-30T12:00:00Z",
        asset_id: "asset-123",
        vault_ref: "never-project-this",
      });
    },
    sessionHeader: "X-Chriptmas-Desktop-Session",
    timeoutSignal: (milliseconds) => ({ milliseconds }),
  });
  controller.install();
  return { calls, controller, handlers };
}

test("LineMap selection recomputes grant proof with the rotated request credential", async () => {
  const session = { origin: "http://127.0.0.1:11495", secret: "old-secret", instance_id: "desktop-1" };
  const value = harness({ session, onUpload: () => { session.secret = "new-secret"; } });
  await value.handlers.get(CHANNEL)(trustedEvent, request);
  assert.equal(value.calls.find(([kind]) => kind === "grant-headers")[2], "new-secret");
  assert.equal(value.calls.find(([kind]) => kind === "fetch")[2].headers["X-Chriptmas-Desktop-Session"], "new-secret");
});

test("LineMap import owns one main-only opaque selection channel", async () => {
  const value = harness();
  assert.deepEqual([...value.handlers.keys()], [CHANNEL]);
  assert.equal(value.controller.install(), false);
  await assert.rejects(value.handlers.get(CHANNEL)({ trusted: false }, request), /ipc_main_sender_rejected/);
  assert.deepEqual(value.calls.map(([name]) => name), ["guard"]);
  assert.equal(value.controller.dispose(), true);
  assert.equal(value.controller.dispose(), false);
  assert.equal(value.handlers.has(CHANNEL), false);
});

test("renderer request is exact, path-free, and rejects malformed source types", async () => {
  const value = harness();
  for (const invalid of [
    { ...request, filePath: "C:\\private\\override.json" },
    { ...request, assetId: "asset-override" },
    { ...request, sourceType: "../escape" },
    { ...request, commandId: "../command" },
  ]) {
    await assert.rejects(value.handlers.get(CHANNEL)(trustedEvent, invalid), /context_graph_import_request_invalid/);
  }
  assert.equal(value.calls.filter(([name]) => name === "dialog").length, 0);
  assert.equal(value.calls.filter(([name]) => name === "grant").length, 0);
});

test("a newly registered opaque source type uses the same desktop chain without a core branch", async () => {
  const value = harness({ selectionResponse: response({
    selection_id: "selection-789",
    project_id: "project-a",
    source_type: "external_agent_graph",
    display_name: "unrecognized.canvas",
    expires_at: "2026-08-30T12:00:00Z",
  }) });
  const result = await value.handlers.get(CHANNEL)(trustedEvent, {
    projectId: "project-a", sourceType: "external_agent_graph", commandId: "command-456",
  });
  assert.equal(result.source_type, "external_agent_graph");
  const dialog = value.calls.find(([name]) => name === "dialog");
  assert.deepEqual(dialog[2].filters, [{ name: "LineMap graph files", extensions: ["*"] }]);
  const grant = value.calls.find(([name]) => name === "grant")[1];
  assert.equal(grant.mediaType, "application/octet-stream");
  assert.equal(JSON.parse(value.calls.find(([name]) => name === "fetch")[2].body).source_type, "external_agent_graph");
});

test("main opens the native picker, uploads its grant, and stages exact sidecar selection input", async () => {
  const value = harness();
  const result = await value.handlers.get(CHANNEL)(trustedEvent, request);
  assert.deepEqual(result, {
    selection_id: "selection-123",
    project_id: "project-a",
    source_type: "thoughtdag",
    display_name: "strategy.thoughtdag.json",
    expires_at: "2026-08-30T12:00:00Z",
  });
  const dialog = value.calls.find(([name]) => name === "dialog");
  assert.deepEqual(dialog.slice(1), ["main-window", {
    title: "选择 LineMap 导入文件",
    properties: ["openFile"],
    filters: [{ name: "LineMap graph files", extensions: ["*"] }],
  }]);
  const grant = value.calls.find(([name]) => name === "grant")[1];
  assert.equal(grant.filePath, "C:\\private\\strategy.thoughtdag.json");
  assert.equal(grant.mediaType, "application/octet-stream");
  assert.equal(grant.sourceKind, "file");
  const fetch = value.calls.find(([name]) => name === "fetch");
  assert.equal(fetch[1], "http://127.0.0.1:11495/api/rebuild/context-graph-import-selections");
  assert.equal(fetch[2].headers["X-Chriptmas-Desktop-Session"], "desktop-secret");
  assert.equal(fetch[2].headers["X-Chriptmas-File-Grant"], "file-grant-signed");
  assert.equal(fetch[2].headers["X-Chriptmas-File-Signature"], "signed-proof");
  assert.equal(Object.hasOwn(fetch[2].headers, "Content-Length"), false);
  assert.equal(fetch[2].signal.milliseconds, 5000);
  assert.deepEqual(JSON.parse(fetch[2].body), {
    project_id: "project-a", source_type: "thoughtdag", command_id: "command-123", asset_id: "asset-123",
  });
});

test("cancellation, session absence, upload drift, and bad backend responses fail closed", async () => {
  const cancelled = harness({ dialogResult: { canceled: true, filePaths: [] } });
  assert.equal(await cancelled.handlers.get(CHANNEL)(trustedEvent, request), null);
  assert.equal(cancelled.calls.some(([name]) => name === "grant"), false);

  const offline = harness({ session: null });
  assert.deepEqual(await offline.handlers.get(CHANNEL)(trustedEvent, request), { status: "unavailable", reason: "sidecar_not_ready" });
  assert.equal(offline.calls.some(([name]) => name === "dialog"), false);

  const badAsset = harness({ uploadResult: { asset_id: "../asset" } });
  await assert.rejects(badAsset.handlers.get(CHANNEL)(trustedEvent, request), /context_graph_import_asset_invalid/);

  const badResponse = harness({ selectionResponse: response({
    selection_id: "selection-123", project_id: "project-b", source_type: "thoughtdag",
    display_name: "strategy.json", expires_at: "2026-08-30T12:00:00Z",
  }) });
  await assert.rejects(badResponse.handlers.get(CHANNEL)(trustedEvent, request), /context_graph_import_selection_response_invalid/);
});

test("renderer result never leaks private path, uploaded asset, vault reference, or session secret", async () => {
  const value = harness();
  const result = await value.handlers.get(CHANNEL)(trustedEvent, request);
  const rendered = JSON.stringify(result);
  for (const secret of ["C:\\private", "asset-123", "never-project-this", "desktop-secret"]) {
    assert.equal(rendered.includes(secret), false, secret);
  }
  assert.deepEqual(Object.keys(result).sort(), ["display_name", "expires_at", "project_id", "selection_id", "source_type"]);
});
