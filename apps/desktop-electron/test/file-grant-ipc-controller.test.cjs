const assert = require("node:assert/strict");
const test = require("node:test");

const { FileGrantIpcController } = require("../src/file-grant-ipc-controller.cjs");

const trustedEvent = Object.freeze({ trusted: true, sender: Object.freeze({ id: 7 }) });

function harness({ dialogResult, session = { instance_id: "instance", secret: "secret", origin: "http://127.0.0.1:1" }, upload, onGrant = () => {} } = {}) {
  const handlers = new Map();
  const removed = [];
  const dialogs = [];
  const grants = [];
  const uploads = [];
  const guarded = [];
  const controllers = [];

  class FakeAbortController {
    constructor() {
      this.signal = { aborted: false, reason: null, listeners: [] };
      this.signal.addEventListener = (_name, listener) => this.signal.listeners.push(listener);
      controllers.push(this);
    }

    abort(reason) {
      if (this.signal.aborted) return;
      this.signal.aborted = true;
      this.signal.reason = reason;
      for (const listener of this.signal.listeners) listener();
    }
  }

  const controller = new FileGrantIpcController({
    ipcMain: {
      handle(channel, handler) { handlers.set(channel, handler); },
      removeHandler(channel) { removed.push(channel); handlers.delete(channel); },
    },
    dialog: {
      async showOpenDialog(options) {
        dialogs.push(options);
        return dialogResult || { canceled: false, filePaths: ["C:\\fixtures\\source.txt"] };
      },
    },
    requireMainRenderer(event) {
      guarded.push(event);
      if (!event?.trusted) throw new Error("ipc_main_sender_rejected");
    },
    sessionProvider: () => session,
    createFileGrant: async (options) => { grants.push(options); onGrant(); return { grant_id: "grant" }; },
    uploadFileGrant: upload || (async (grant, active, options) => {
      uploads.push({ grant, active, options });
      return { status: "uploaded" };
    }),
    createAbortController: () => new FakeAbortController(),
  });
  controller.install();
  return { controller, controllers, dialogs, grants, guarded, handlers, removed, uploads };
}

test("file upload reads the renewed session after grant creation", async () => {
  const session = { instance_id: "instance", secret: "old-secret", origin: "http://127.0.0.1:1" };
  const value = harness({ session, onGrant: () => { session.secret = "new-secret"; } });
  await value.handlers.get("chriptmas:upload-local-file")(trustedEvent, { filePath: "C:\\source.txt", requestId: "request-12345678" });
  assert.equal(value.uploads[0].active.secret, "new-secret");
});

test("file upload refuses a replaced sidecar instance before sending bytes", async () => {
  const session = { instance_id: "instance", secret: "old-secret", origin: "http://127.0.0.1:1" };
  const value = harness({ session, onGrant: () => { session.instance_id = "replacement"; } });
  await assert.rejects(value.handlers.get("chriptmas:upload-local-file")(trustedEvent, {
    filePath: "C:\\source.txt", requestId: "request-12345678",
  }), /desktop_session_instance_changed/);
  assert.deepEqual(value.uploads, []);
});

test("file grant controller owns the exact three-channel IPC surface", () => {
  const value = harness();
  assert.deepEqual([...value.handlers.keys()].sort(), [
    "chriptmas:cancel-file-upload",
    "chriptmas:select-local-file",
    "chriptmas:upload-local-file",
  ]);
});

test("every file grant handler rejects an unknown renderer before work", async () => {
  const value = harness();
  for (const [channel, handler] of value.handlers) {
    await assert.rejects(async () => handler({ trusted: false, sender: { id: 99 } }, {}), /ipc_main_sender_rejected/, channel);
  }
  assert.equal(value.guarded.length, value.handlers.size);
  assert.deepEqual(value.dialogs, []);
  assert.deepEqual(value.grants, []);
});

test("native picker keeps fixed media filters and cancellation semantics", async () => {
  const image = harness();
  assert.equal(await image.handlers.get("chriptmas:select-local-file")(trustedEvent, { mediaKind: "image" }), "C:\\fixtures\\source.txt");
  assert.deepEqual(image.dialogs[0], {
    title: "选择本地文件",
    properties: ["openFile"],
    filters: [{ name: "Images", extensions: ["png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"] }],
  });
  const cancelled = harness({ dialogResult: { canceled: true, filePaths: [] } });
  assert.equal(await cancelled.handlers.get("chriptmas:select-local-file")(trustedEvent, {}), null);
});

test("upload validates path session and request identity before creating a grant", async () => {
  const value = harness({ session: null });
  const handler = value.handlers.get("chriptmas:upload-local-file");
  await assert.rejects(handler(trustedEvent, { filePath: "", requestId: "request-12345678" }), /file_grant_path_invalid/);
  await assert.rejects(handler(trustedEvent, { filePath: "C:\\source.txt", requestId: "request-12345678" }), /file_grant_sidecar_unavailable/);
  assert.deepEqual(value.grants, []);

  const ready = harness();
  await assert.rejects(ready.handlers.get("chriptmas:upload-local-file")(trustedEvent, {
    filePath: "C:\\source.txt",
    requestId: "short",
  }), /file_grant_request_invalid/);
});

test("active upload is request-unique sender-cancellable and reusable after cleanup", async () => {
  const value = harness({
    upload: (_grant, _session, { signal }) => new Promise((resolve, reject) => {
      signal.addEventListener("abort", () => reject(signal.reason));
    }),
  });
  const uploadHandler = value.handlers.get("chriptmas:upload-local-file");
  const cancelHandler = value.handlers.get("chriptmas:cancel-file-upload");
  const request = { filePath: "C:\\source.txt", requestId: "request-12345678" };
  const pending = uploadHandler(trustedEvent, request);
  await new Promise((resolve) => setImmediate(resolve));
  await assert.rejects(uploadHandler(trustedEvent, request), /file_grant_request_invalid/);
  assert.deepEqual(cancelHandler({ trusted: true, sender: { id: 8 } }, { requestId: request.requestId }), { status: "not_found" });
  assert.deepEqual(cancelHandler(trustedEvent, { requestId: request.requestId }), { status: "cancelled" });
  await assert.rejects(pending, /file_grant_upload_cancelled/);

  const retried = uploadHandler(trustedEvent, request);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(cancelHandler(trustedEvent, { requestId: request.requestId }), { status: "cancelled" });
  await assert.rejects(retried, /file_grant_upload_cancelled/);
});

test("dispose aborts active work removes owned handlers and is idempotent", async () => {
  const value = harness({
    upload: (_grant, _session, { signal }) => new Promise((resolve, reject) => {
      signal.addEventListener("abort", () => reject(signal.reason));
    }),
  });
  const pending = value.handlers.get("chriptmas:upload-local-file")(trustedEvent, {
    filePath: "C:\\source.txt",
    requestId: "request-12345678",
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(value.controller.dispose(), true);
  assert.equal(value.controller.dispose(), false);
  await assert.rejects(pending, /app_quit/);
  assert.equal(value.handlers.size, 0);
  assert.deepEqual(value.removed.sort(), [
    "chriptmas:cancel-file-upload",
    "chriptmas:select-local-file",
    "chriptmas:upload-local-file",
  ]);
});
