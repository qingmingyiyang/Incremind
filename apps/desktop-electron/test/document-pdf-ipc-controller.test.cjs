const assert = require("node:assert/strict");
const test = require("node:test");

const {
  CHANNEL, DocumentPdfIpcController, PRINT_OPTIONS, SLIDE_PRINT_OPTIONS,
  frozenProfileMatches,
} = require("../src/document-pdf-ipc-controller.cjs");

function jsonResponse(payload, status = 200) { return { ok: status >= 200 && status < 300, status, async json() { return payload; } }; }
function bytesResponse(bytes, status = 200) { return { ok: status === 200, status, async arrayBuffer() { return Uint8Array.from(bytes).buffer; } }; }

function harness({ session = { origin: "http://127.0.0.1:11495", secret: "session-secret", instance_id: "instance-1" }, onFetch = () => {} } = {}) {
  const calls = []; const handlers = new Map(); const windows = [];
  const responses = [
    jsonResponse({ operation_id: "pdf-op-1", status: "waiting_for_electron" }),
    jsonResponse({ operation_id: "pdf-op-1", status: "claimed", claim_token: "x".repeat(32), profile: {
      profile_id: "builtin.a4-document", revision: 1, renderer_id: "electron.webcontents.print-to-pdf",
      renderer_revision: 1, electron_major: 43, print_options: PRINT_OPTIONS,
    } }),
    jsonResponse({ operation_id: "pdf-op-1", html: "<!DOCTYPE html><html><head></head><body>Exact</body></html>" }),
    jsonResponse({ operation_id: "pdf-op-1", status: "completed", replayed: false }),
    jsonResponse({ operation_id: "pdf-slides-1", status: "waiting_for_electron" }),
    jsonResponse({ operation_id: "pdf-slides-1", status: "claimed", claim_token: "y".repeat(32), profile: {
      profile_id: "builtin.slide-document", revision: 1, renderer_id: "electron.webcontents.print-to-pdf",
      renderer_revision: 1, electron_major: 43, print_options: SLIDE_PRINT_OPTIONS,
    } }),
    jsonResponse({ operation_id: "pdf-slides-1", html: "<!DOCTYPE html><html><head></head><body>Slides</body></html>" }),
    jsonResponse({ operation_id: "pdf-slides-1", status: "completed", replayed: false }),
  ];
  class FakeWindow {
    constructor(options) { this.options = options; this.destroyed = false; windows.push(this); this.webContents = {
      setWindowOpenHandler: (handler) => { calls.push(["window-open", handler()]); },
      on: (name, handler) => { calls.push(["web-on", name]); if (name === "will-navigate") this.navigate = handler; },
      printToPDF: async (options) => { calls.push(["print", options]); return Buffer.from("%PDF-exact"); },
    }; }
    async loadURL(url) { calls.push(["load", url]); }
    isDestroyed() { return this.destroyed; }
    destroy() { this.destroyed = true; calls.push(["destroy"]); }
  }
  const controller = new DocumentPdfIpcController({
    ipcMain: { handle: (channel, handler) => handlers.set(channel, handler), removeHandler: (channel) => handlers.delete(channel) },
    BrowserWindow: FakeWindow,
    requireMainRenderer: (event) => { calls.push(["guard"]); if (!event?.trusted) throw new Error("rejected"); },
    sessionProvider: () => session,
    versionsProvider: () => ({ electron: "43.0.0", chrome: "144.0.0.0" }),
    fetchImpl: async (url, options) => { calls.push(["fetch", url, options]); onFetch(url, options, calls); return responses.shift(); },
    cryptoApi: { createHmac: (_algorithm, secret) => ({ update(message) { calls.push(["sign", secret, message]); return this; }, digest() { return "signed"; } }) },
    sessionHeader: "X-Chriptmas-Session", timeoutSignal: (milliseconds) => ({ milliseconds }),
  });
  controller.install(); return { calls, controller, handlers, windows };
}

test("main-only controller freezes the PDF profile and prints verified sidecar HTML", async () => {
  const value = harness();
  const result = await value.handlers.get(CHANNEL)({ trusted: true }, { htmlDeliveryId: "delivery-1" });
  assert.equal(result.status, "completed");
  assert.equal(result.operation_id, "pdf-op-1");
  assert.equal(result.slide_operation_id, "pdf-slides-1");
  const fetches = value.calls.filter(([name]) => name === "fetch");
  assert.equal(fetches[0][1], "http://127.0.0.1:11495/api/rebuild/document-deliveries/delivery-1/pdf-operations");
  assert.equal(JSON.parse(fetches[0][2].body).profile_id, "builtin.a4-document");
  assert.equal(fetches[1][2].headers["X-Chriptmas-Main-Signature"], "signed");
  assert.equal(fetches[2][2].headers["X-Chriptmas-Pdf-Claim"], "x".repeat(32));
  assert.deepEqual(value.calls.find(([name]) => name === "print")[1], PRINT_OPTIONS);
  assert.deepEqual(value.calls.filter(([name]) => name === "print")[1][1], SLIDE_PRINT_OPTIONS);
  assert.equal(value.windows[0].options.show, false);
  assert.equal(value.windows[0].options.webPreferences.sandbox, true);
  assert.equal(value.windows[0].options.webPreferences.nodeIntegration, false);
  assert.equal(value.windows[0].destroyed, true);
  assert.match(JSON.parse(fetches[3][2].body).pdf_base64, /^JVBER/);
  assert.equal(fetches[3][2].headers["X-Chriptmas-Pdf-Claim"], "x".repeat(32));
  assert.equal("claim_token" in JSON.parse(fetches[3][2].body), false);
  assert.ok(value.calls.some((call) => call[0] === "sign" && call[2] === "document-pdf:POST:/api/rebuild/desktop/document-pdf-operations/pdf-op-1/claim"));
  assert.equal(JSON.parse(fetches[4][2].body).profile_id, "builtin.slide-document");
});

test("PDF preparation carries the active project for both formats", async () => {
  const value = harness();
  const result = await value.handlers.get(CHANNEL)({ trusted: true }, {
    htmlDeliveryId: "delivery-1", projectId: "project-alpha",
  });
  assert.equal(result.status, "completed");
  const fetches = value.calls.filter(([name]) => name === "fetch");
  assert.equal(fetches[0][1], "http://127.0.0.1:11495/api/rebuild/document-deliveries/delivery-1/pdf-operations?project_id=project-alpha");
  assert.equal(fetches[4][1], fetches[0][1]);
});

test("PDF follow-up requests use the rotated credential for both authentication and main signature", async () => {
  const session = { origin: "http://127.0.0.1:11495", secret: "old-secret", instance_id: "instance-1" };
  const value = harness({ session, onFetch: (_url, _options, calls) => {
    if (calls.filter(([kind]) => kind === "fetch").length === 1) session.secret = "new-secret";
  } });
  assert.equal((await value.handlers.get(CHANNEL)({ trusted: true }, { htmlDeliveryId: "delivery-1" })).status, "completed");
  const fetches = value.calls.filter(([kind]) => kind === "fetch");
  assert.equal(fetches[0][2].headers["X-Chriptmas-Session"], "old-secret");
  assert.equal(fetches[1][2].headers["X-Chriptmas-Session"], "new-secret");
  const claimSign = value.calls.find((call) => call[0] === "sign" && call[2].endsWith("/claim"));
  assert.equal(claimSign[1], "new-secret");
});

test("frozen PDF profile comparison ignores JSON key order but rejects field drift", () => {
  const reordered = {
    electron_major: 43,
    print_options: {
      margins: { right: 0.55, left: 0.55, bottom: 0.5, top: 0.5 },
      printBackground: true,
      preferCSSPageSize: false,
      pageSize: "A4",
      landscape: false,
      displayHeaderFooter: false,
    },
    renderer_revision: 1,
    renderer_id: "electron.webcontents.print-to-pdf",
    revision: 1,
    profile_id: "builtin.a4-document",
  };
  assert.equal(frozenProfileMatches(reordered), true);
  assert.equal(frozenProfileMatches({ ...reordered, extra: true }), false);
  assert.equal(frozenProfileMatches({ ...reordered, print_options: { ...reordered.print_options, extra: true } }), false);
  assert.equal(frozenProfileMatches({ ...reordered, electron_major: 44 }), false);
  assert.equal(frozenProfileMatches({
    ...reordered,
    profile_id: "builtin.slide-document",
    print_options: SLIDE_PRINT_OPTIONS,
  }), true);
});

test("invalid delivery and unavailable sidecar stop before hidden window", async () => {
  const invalid = harness();
  assert.deepEqual(await invalid.handlers.get(CHANNEL)({ trusted: true }, { htmlDeliveryId: "../bad" }), { status: "rejected", reason: "html_delivery_id_invalid" });
  assert.deepEqual(await invalid.handlers.get(CHANNEL)({ trusted: true }, { htmlDeliveryId: "delivery-1", projectId: "../bad" }), { status: "rejected", reason: "project_id_invalid" });
  assert.equal(invalid.windows.length, 0);
  const offline = harness({ session: null });
  assert.deepEqual(await offline.handlers.get(CHANNEL)({ trusted: true }, { htmlDeliveryId: "delivery-1" }), { status: "unavailable", reason: "sidecar_not_ready" });
  assert.equal(offline.windows.length, 0);
});

test("dispose removes IPC and destroys an active hidden window", () => {
  const value = harness();
  const active = new value.controller.BrowserWindow({});
  value.controller.activeWindow = active;
  assert.equal(value.controller.dispose(), true);
  assert.equal(active.destroyed, true);
  assert.equal(value.handlers.has(CHANNEL), false);
});
