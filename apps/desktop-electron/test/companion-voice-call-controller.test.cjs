const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { CompanionVoiceCallController, CompanionVoiceCallPresentationController, cleanOwnedVoiceFiles, requireAudio } = require("../src/companion/voice-call-controller.cjs");

const WEBM = Buffer.concat([Buffer.from([0x1a, 0x45, 0xdf, 0xa3]), Buffer.alloc(32, 7)]);

test("voice call uses signed grant, bounded endpoint, and removes ephemeral audio", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "voice-call-test-"));
  const session = { origin: "http://127.0.0.1:4567", secret: "secret", instance_id: "instance" };
  let grantedPath = "";
  const calls = [];
  const controller = new CompanionVoiceCallController({
    tempRoot: root,
    sessionProvider: () => session,
    createFileGrantFn: async ({ filePath, mediaType, sourceKind }) => {
      grantedPath = filePath;
      assert.equal(mediaType, "audio/webm");
      assert.equal(sourceKind, "audio");
      assert.deepEqual(fs.readFileSync(filePath), WEBM);
      return { local: true };
    },
    uploadFileGrantFn: async (_grant, active, options) => {
      assert.deepEqual(active, session);
      assert.equal(options.endpointPath, "/api/rebuild/companion/voice/grants");
      return { grant: { grant_id: "grant-1" } };
    },
    fetchFn: async (url, options) => {
      calls.push({ url, options });
      return { ok: true, status: 200, json: async () => ({ result: { text: "你好", language: "zh" } }) };
    },
  });
  const result = await controller.transcribe({ request_id: "voice:12345678-abcd", media_type: "audio/webm", bytes: WEBM });
  assert.equal(result.text, "你好");
  assert.match(calls[0].url, /\/voice\/transcribe$/);
  assert.deepEqual(JSON.parse(calls[0].options.body), { request_id: "voice:12345678-abcd", grant_id: "grant-1" });
  assert.equal(fs.existsSync(grantedPath), false);
  assert.deepEqual(fs.readdirSync(root), []);
  fs.rmSync(root, { recursive: true, force: true });
});

test("voice transcription uses the renewed session after a grant upload", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "voice-rotation-test-"));
  try {
    let session = { origin: "http://127.0.0.1:4567", secret: "old-secret", instance_id: "instance" };
    const seen = [];
    const controller = new CompanionVoiceCallController({
      tempRoot: root,
      sessionProvider: () => session,
      createFileGrantFn: async () => ({ grant_id: "file-grant" }),
      uploadFileGrantFn: async (_grant, active) => {
        seen.push(active.secret);
        session = { ...session, secret: "new-secret" };
        return { grant: { grant_id: "grant-1" } };
      },
      fetchFn: async (_url, options) => {
        seen.push(options.headers["X-Chriptmas-Desktop-Session"]);
        return { ok: true, status: 200, json: async () => ({ result: { text: "完成" } }) };
      },
    });
    assert.equal((await controller.transcribe({ request_id: "voice:12345678-abcd", media_type: "audio/webm", bytes: WEBM })).text, "完成");
    assert.deepEqual(seen, ["old-secret", "new-secret"]);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("voice call rejects mismatched audio and cleans only owned regular files", () => {
  assert.throws(() => requireAudio(Buffer.alloc(40), "audio/webm"), /audio_invalid/);
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "voice-clean-test-"));
  const owned = path.join(root, "voice-recording-12345678-1234-4123-8123-123456789abc.webm");
  const unrelated = path.join(root, "keep.webm");
  fs.writeFileSync(owned, WEBM);
  fs.writeFileSync(unrelated, WEBM);
  cleanOwnedVoiceFiles(root);
  assert.equal(fs.existsSync(owned), false);
  assert.equal(fs.existsSync(unrelated), true);
  fs.rmSync(root, { recursive: true, force: true });
});

test("voice presentation waits for synthesis and matching playback end", async () => {
  let finishSynthesis;
  const synthesized = new Promise((resolve) => { finishSynthesis = resolve; });
  const dispatch = [];
  const voice = { status: () => ({ enabled: true }), speak: async () => synthesized, cancel: () => {} };
  const controller = new CompanionVoiceCallPresentationController({ voiceProvider: () => voice, isQuiet: () => false, dispatchAudio: (audio, id) => { dispatch.push({ audio, id }); return true; }, cancelAudio: () => {}, setTimer: () => 1, clearTimer: () => {} });
  let settled = false;
  const result = controller.present({ request_id: "voice:12345678", text: "测试回复" }).finally(() => { settled = true; });
  await Promise.resolve(); assert.equal(settled, false); assert.deepEqual(dispatch, []);
  finishSynthesis({ audio: Buffer.alloc(44) }); await Promise.resolve(); await Promise.resolve();
  assert.equal(settled, false); assert.equal(dispatch[0].id, "voice:12345678");
  assert.equal(controller.acknowledge("voice:other", "ended"), false);
  assert.equal(controller.acknowledge("voice:12345678", "playing"), true); assert.equal(settled, false);
  controller.acknowledge("voice:12345678", "ended");
  assert.deepEqual(await result, { status: "completed" });
});

test("voice presentation handles disabled, quiet, dispatch failure and cancellation", async () => {
  const disabled = new CompanionVoiceCallPresentationController({ voiceProvider: () => ({ status: () => ({ enabled: false }) }), isQuiet: () => false, dispatchAudio: () => true, cancelAudio: () => {} });
  assert.deepEqual(await disabled.present({ request_id: "voice:12345678", text: "测试" }), { status: "skipped", reason: "disabled" });
  const quiet = new CompanionVoiceCallPresentationController({ voiceProvider: () => ({ status: () => ({ enabled: true }) }), isQuiet: () => true, dispatchAudio: () => true, cancelAudio: () => {} });
  assert.deepEqual(await quiet.present({ request_id: "voice:12345678", text: "测试" }), { status: "skipped", reason: "quiet" });
  let resolve; const voice = { status: () => ({ enabled: true }), speak: () => new Promise((value) => { resolve = value; }), cancel: () => {} };
  const cancelled = new CompanionVoiceCallPresentationController({ voiceProvider: () => voice, isQuiet: () => false, dispatchAudio: () => true, cancelAudio: () => {} });
  const pending = cancelled.present({ request_id: "voice:12345678", text: "测试" }); cancelled.cancel(); resolve({ audio: Buffer.alloc(44) });
  await assert.rejects(pending, /cancelled/);
  const failed = new CompanionVoiceCallPresentationController({ voiceProvider: () => ({ status: () => ({ enabled: true }), speak: async () => ({ audio: Buffer.alloc(44) }) }), isQuiet: () => false, dispatchAudio: () => false, cancelAudio: () => {} });
  await assert.rejects(failed.present({ request_id: "voice:12345678", text: "测试" }), /tts_failed/);
});

test("new presentation and stale timeout cannot strand or settle a later playback", async () => {
  const timers = [];
  const voice = { status: () => ({ enabled: true }), speak: async (text) => ({ audio: Buffer.from(text) }), cancel: () => {} };
  const controller = new CompanionVoiceCallPresentationController({ voiceProvider: () => voice, isQuiet: () => false, dispatchAudio: () => true, cancelAudio: () => {}, setTimer: (fn) => { timers.push(fn); return timers.length; }, clearTimer: () => {} });
  const first = controller.present({ request_id: "voice:11111111", text: "first" });
  await Promise.resolve(); await Promise.resolve();
  const second = controller.present({ request_id: "voice:22222222", text: "second" });
  await assert.rejects(first, /cancelled/);
  await Promise.resolve(); await Promise.resolve();
  timers[0]();
  assert.equal(controller.acknowledge("voice:11111111", "ended"), false);
  assert.equal(controller.acknowledge("voice:22222222", "ended"), true);
  assert.deepEqual(await second, { status: "completed" });
});
