const { currentSessionForRequest } = require("../src/desktop-session.cjs");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { createFileGrant, grantHeaders, signGrant, uploadFileGrant } = require("../src/file-grant.cjs");

const session = {
  secret: "s".repeat(43),
  instance_id: "instance-test",
  origin: "http://127.0.0.1:8317",
};

test("each request uses a rotated secret only for the same sidecar instance and origin", () => {
  let current = session;
  const provider = () => current;
  assert.equal(currentSessionForRequest(provider, session), session);
  current = { ...session, secret: "rotated-secret" };
  assert.equal(currentSessionForRequest(provider, session).secret, "rotated-secret");
  current = { ...current, instance_id: "other-instance" };
  assert.throws(() => currentSessionForRequest(provider, session), /desktop_session_instance_changed/);
  current = { ...session, origin: "http://127.0.0.1:8318" };
  assert.throws(() => currentSessionForRequest(provider, session), /desktop_session_instance_changed/);
  current = null;
  assert.throws(() => currentSessionForRequest(provider, session), /file_grant_sidecar_unavailable/);
});

test("grant hashes a temporary file without exposing its path in headers", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-file-grant-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const filePath = path.join(root, "large-video.mp4");
  const content = crypto.randomBytes(2 * 1024 * 1024);
  fs.writeFileSync(filePath, content);

  const grant = await createFileGrant({ filePath, session, mediaType: "video/mp4", sourceKind: "video" });
  const headers = grantHeaders(grant, session.secret);

  assert.equal(grant.sha256, crypto.createHash("sha256").update(content).digest("hex"));
  assert.equal(grant.size_bytes, content.length);
  assert.equal(headers["X-Chriptmas-File-Size"], String(content.length));
  assert.equal(Object.values(headers).includes(filePath), false);
});

test("grant hashes a 128 MiB file with bounded process memory", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-file-grant-large-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const filePath = path.join(root, "two-hour-budget-fixture.bin");
  const descriptor = fs.openSync(filePath, "w");
  try { fs.ftruncateSync(descriptor, 128 * 1024 * 1024); }
  finally { fs.closeSync(descriptor); }
  const beforeRss = process.memoryUsage().rss;

  const grant = await createFileGrant({
    filePath,
    session,
    mediaType: "application/octet-stream",
    sourceKind: "file",
  });

  const rssGrowth = process.memoryUsage().rss - beforeRss;
  assert.equal(grant.size_bytes, 128 * 1024 * 1024);
  assert.ok(rssGrowth < 64 * 1024 * 1024, `streaming hash RSS grew by ${rssGrowth} bytes`);
});

test("upload fails closed when file identity changes after grant", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-file-grant-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const filePath = path.join(root, "moving.bin");
  fs.writeFileSync(filePath, Buffer.alloc(1024 * 1024, 1));
  const grant = await createFileGrant({ filePath, session, mediaType: "application/octet-stream", sourceKind: "file" });
  fs.appendFileSync(filePath, Buffer.from("changed"));
  await assert.rejects(uploadFileGrant(grant, session), /file_grant_file_changed/);
});

test("custom grant endpoints cannot escape the authenticated loopback origin", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-file-grant-endpoint-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const filePath = path.join(root, "screen.jpg"); fs.writeFileSync(filePath, Buffer.from("fixture"));
  const grant = await createFileGrant({ filePath, session, mediaType: "image/jpeg", sourceKind: "image" });
  for (const endpointPath of ["//evil.example/upload", "/safe\\evil", "/safe?redirect=1", "https://evil.example/upload"]) {
    await assert.rejects(uploadFileGrant(grant, session, { endpointPath }), /endpoint_invalid/);
  }
});

test("grant signature matches the Python sidecar canonical contract", () => {
  assert.equal(signGrant({
    grant_id: "file-grant-" + "a".repeat(43),
    session_instance_id: "instance-test",
    display_name: "two-hour-video.mp4",
    media_type: "video/mp4",
    source_kind: "video",
    size_bytes: 8 * 1024 * 1024,
    sha256: "b".repeat(64),
    expires_at_ms: 1720000060000,
  }, "s".repeat(43)), "64c9c3952f1ab67f8d53595d11b221e40d863b2ed135688d95eb895d22ca2e47");
});

test("preload keeps the path behind IPC and main delegates to a guarded controller", () => {
  const preload = fs.readFileSync(path.join(__dirname, "..", "src", "preload.cjs"), "utf8");
  const main = fs.readFileSync(path.join(__dirname, "..", "src", "main.cjs"), "utf8");
  const controller = fs.readFileSync(path.join(__dirname, "..", "src", "file-grant-ipc-controller.cjs"), "utf8");
  assert.match(preload, /webUtils\.getPathForFile\(file\)/);
  assert.match(preload, /ipcRenderer\.invoke\("chriptmas:upload-local-file"/);
  assert.match(main, /event\.sender\.id !== desktopWindowRegistry\.main\.webContents\.id/);
  assert.match(main, /new FileGrantIpcController\(\{[\s\S]*?requireMainRenderer,[\s\S]*?sessionProvider:/);
  assert.match(controller, /require\("\.\/file-grant\.cjs"\)/);
  assert.match(controller, /this\.requireMainRenderer\(event\)/);
  assert.match(controller, /this\.createFileGrant/);
  assert.match(controller, /this\.uploadFileGrant/);
});
