const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const test = require("node:test");
const { CompanionMultiCharacterLink, CompanionMultiCharacterSettings, normalizeMessage } = require("../src/companion/multicharacter-link.cjs");
const { createTemporaryRootTracker } = require("./support/temporary-root.cjs");

const temporaryRoot = createTemporaryRootTracker(test);

function root() { return temporaryRoot("chriptmas-link-"); }
function message(type, extra = {}) { return { protocol_version: 1, instance_id: "a".repeat(36), character_id: "peer.character", message_id: "00000000-0000-4000-8000-000000000001", type, ...extra }; }

test("protocol accepts only closed event schemas and rejects active content", () => {
  assert.equal(normalizeMessage(message("state_changed", { state: "busy" })).state, "busy");
  assert.equal(normalizeMessage(message("emote", { emote: "wave" })).emote, "wave");
  assert.equal(normalizeMessage(message("short_line", { text: "你好，今天也一起加油。" })).text, "你好，今天也一起加油。");
  for (const text of ["<b>hello</b>", "https://example.com", "C:\\secret.txt", "run powershell", "system prompt", "a".repeat(201)]) {
    assert.throws(() => normalizeMessage(message("short_line", { text })), /companion_link_text_rejected/);
  }
  assert.throws(() => normalizeMessage({ ...message("hello"), command: "open" }), /message_invalid/);
});

test("two opted-in compatible characters discover and exchange bounded events", async (t) => {
  const shared = root(); const received = [];
  const first = new CompanionMultiCharacterLink({ root: shared, characterId: "chriptmas.bear", allowedCharacterIds: ["friend.cat"] });
  const second = new CompanionMultiCharacterLink({ root: shared, characterId: "friend.cat", allowedCharacterIds: ["chriptmas.bear"], onEvent: (event) => received.push(event) });
  t.after(async () => { await first.stop(); await second.stop(); });
  await first.start(); await second.start();

  const peer = first.peers()[0];
  assert.equal(peer.character_id, "friend.cat");
  const result = await first.send(peer.instance_id, { type: "short_line", text: "要一起喝下午茶吗？" });

  assert.equal(result.status, "accepted");
  assert.deepEqual(received, [{ type: "short_line", character_id: "chriptmas.bear", text: "要一起喝下午茶吗？" }]);
  assert.equal(JSON.stringify(first.status()).includes("token"), false);
});

test("unknown characters are invisible and quiet mode drops short lines", async (t) => {
  const shared = root(); const received = [];
  const first = new CompanionMultiCharacterLink({ root: shared, characterId: "chriptmas.bear", allowedCharacterIds: ["friend.cat"] });
  const second = new CompanionMultiCharacterLink({ root: shared, characterId: "friend.cat", allowedCharacterIds: ["chriptmas.bear"], quiet: () => true, onEvent: (event) => received.push(event) });
  const stranger = new CompanionMultiCharacterLink({ root: shared, characterId: "stranger.fox", allowedCharacterIds: [] });
  t.after(async () => { await first.stop(); await second.stop(); await stranger.stop(); });
  await first.start(); await second.start(); await stranger.start();
  assert.deepEqual(first.peers().map((item) => item.character_id), ["friend.cat"]);
  await first.send(first.peers()[0].instance_id, { type: "short_line", text: "不会在安静模式显示" });
  assert.deepEqual(received, []);
});

test("bad token replay and seventh message per minute fail closed", async (t) => {
  const shared = root(); const first = new CompanionMultiCharacterLink({ root: shared, characterId: "chriptmas.bear", allowedCharacterIds: ["friend.cat"] });
  const second = new CompanionMultiCharacterLink({ root: shared, characterId: "friend.cat", allowedCharacterIds: ["chriptmas.bear"] });
  t.after(async () => { await first.stop(); await second.stop(); }); await first.start(); await second.start();
  const peer = first.peers()[0];
  const fixed = message("emote", { emote: "wave" });
  fixed.instance_id = first.instanceId; fixed.character_id = "chriptmas.bear";
  assert.equal((await post(second.port, second.token, fixed)).statusCode, 200);
  assert.equal((await post(second.port, second.token, fixed)).statusCode, 429);
  assert.equal((await post(second.port, "wrong-token", { ...fixed, message_id: "00000000-0000-4000-8000-000000000002" })).statusCode, 403);
  for (let index = 0; index < 5; index += 1) assert.equal((await first.send(peer.instance_id, { type: "emote", emote: "wave" })).status, "accepted");
  await assert.rejects(() => first.send(peer.instance_id, { type: "emote", emote: "wave" }), /peer_rejected/);
});

test("incompatible versions allow hello but reject state text and emote", async (t) => {
  const shared = root(); const link = new CompanionMultiCharacterLink({ root: shared, characterId: "friend.cat", allowedCharacterIds: ["chriptmas.bear"] });
  t.after(() => link.stop()); await link.start();
  const base = { ...message("hello"), protocol_version: 99, instance_id: "c".repeat(36), character_id: "chriptmas.bear" };
  assert.equal((await post(link.port, link.token, base)).statusCode, 200);
  assert.equal((await post(link.port, link.token, { ...base, type: "state_changed", state: "idle", message_id: "00000000-0000-4000-8000-000000000003" })).statusCode, 409);
});

test("stop removes owned discovery and token while stale strict files are cleaned", async () => {
  const shared = root(); let now = 1000;
  const link = new CompanionMultiCharacterLink({ root: shared, characterId: "chriptmas.bear", now: () => now });
  await link.start(); assert.equal(fs.readdirSync(shared).length, 2);
  await link.stop(); assert.deepEqual(fs.readdirSync(shared), []);
  const instance = "b".repeat(36);
  fs.writeFileSync(path.join(shared, `instance-${instance}.json`), JSON.stringify({ expires_at: 1 }));
  fs.writeFileSync(path.join(shared, `instance-${instance}.token`), "token");
  now = 2000; link.cleanup(); assert.deepEqual(fs.readdirSync(shared), []);
});

test("cleanup never follows or removes a strict-name symlink", async (t) => {
  const shared = root(); const outside = path.join(root(), "outside.json"); fs.writeFileSync(outside, "outside");
  const instance = "d".repeat(36); const record = path.join(shared, `instance-${instance}.json`); const token = path.join(shared, `instance-${instance}.token`);
  try { fs.symlinkSync(outside, record, "file"); } catch { t.skip("symlink creation unavailable"); return; }
  fs.writeFileSync(token, "token");
  const link = new CompanionMultiCharacterLink({ root: shared, characterId: "chriptmas.bear" });
  link.cleanup();
  assert.equal(fs.lstatSync(record).isSymbolicLink(), true); assert.equal(fs.readFileSync(outside, "utf8"), "outside"); assert.equal(fs.existsSync(token), true);
});

test("discovery publishes no bearer token path pid or user identity", async (t) => {
  const shared = root(); const link = new CompanionMultiCharacterLink({ root: shared, characterId: "chriptmas.bear" });
  t.after(() => link.stop()); await link.start();
  const record = fs.readFileSync(path.join(shared, fs.readdirSync(shared).find((name) => name.endsWith(".json"))), "utf8");
  assert.doesNotMatch(record, /bearer|token"|path|pid|username|userData/i);
  assert.match(record, /token_hash/);
});

function post(port, token, value) {
  const body = Buffer.from(JSON.stringify(value));
  return new Promise((resolve, reject) => {
    const request = http.request({ hostname: "127.0.0.1", port, path: "/v1/events", method: "POST", headers: { Host: `127.0.0.1:${port}`, Authorization: `Bearer ${token}`, "Content-Type": "application/json", "Content-Length": body.length } }, (response) => {
      response.resume(); response.on("end", () => resolve({ statusCode: response.statusCode }));
    });
    request.on("error", reject); request.end(body);
  });
}

test("settings are default-off revisioned consented and fail closed when corrupt", () => {
  const directory = root(); const statePath = path.join(directory, "settings.json"); const settings = new CompanionMultiCharacterSettings({ statePath });
  assert.deepEqual(settings.read(), { schema_version: 1, enabled: false, consented: false, character_id: "chriptmas.bear", allowed_character_ids: [], revision: 0 });
  const saved = settings.save({ enabled: true, consented: true, characterId: "chriptmas.bear", allowedCharacterIds: ["friend.cat"], expectedRevision: 0 });
  assert.equal(saved.enabled, true); assert.equal(saved.revision, 1);
  assert.throws(() => settings.save({ enabled: false, consented: true, characterId: "chriptmas.bear", allowedCharacterIds: [], expectedRevision: 0 }), /settings_conflict/);
  fs.writeFileSync(statePath, "bad-json"); assert.throws(() => settings.read(), /settings_invalid/);
});

test("discovery refresh observes only the fixed coarse state enum", async (t) => {
  const shared = root(); let state = "busy"; const link = new CompanionMultiCharacterLink({ root: shared, characterId: "chriptmas.bear", stateProvider: () => state });
  t.after(() => link.stop()); await link.start(); assert.equal(link.status().state, "busy");
  state = "not-a-state"; assert.throws(() => link._publish(), /state_invalid/);
});
