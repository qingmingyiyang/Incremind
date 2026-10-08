const assert = require("node:assert/strict");
const test = require("node:test");

const { CompanionMediaSessionRuntimeController, DELAYS } = require("../src/companion/media-session-runtime-controller.cjs");

function deferred() { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; }
function fixture(overrides = {}) {
  const projections = [], commentary = [], errors = [], timers = [], cleared = [];
  const adapter = overrides.adapter || { sample: async () => ({ status: "empty", title: "", artist: "", playback_status: "closed" }), cancel: () => false };
  const controller = new CompanionMediaSessionRuntimeController({
    readStatus: overrides.readStatus || (async () => ({ config: { enabled: false } })),
    observe: overrides.observe || (async () => ({ result: { status: "empty", title: "", artist: "", playback_status: "closed", commentary: null, commentary_source: null } })),
    adapter,
    onProjection: (value) => projections.push(value), onCommentary: (value) => commentary.push(value), onError: (error) => errors.push(error),
    quiet: overrides.quiet || (() => false), now: () => new Date("2026-07-23T00:00:00Z"),
    setTimer: (handler, delay) => { const item = { handler, delay }; timers.push(item); return item; }, clearTimer: (item) => cleared.push(item),
  });
  return { controller, projections, commentary, errors, timers, cleared, adapter };
}

test("disabled controller owns no sampler and projects disabled", async () => {
  let sampled = 0;
  const current = fixture({ adapter: { sample: async () => { sampled += 1; }, cancel: () => false } });
  await current.controller.refreshConfig();
  assert.equal(sampled, 0);
  assert.equal(current.timers.length, 0);
  assert.equal(current.controller.current().status, "disabled");
});

test("enabled controller samples immediately and sends no source metadata", async () => {
  let request;
  const current = fixture({
    readStatus: async () => ({ config: { enabled: true } }),
    adapter: { sample: async () => ({ status: "ready", source: "private.player", title: "Song", artist: "Artist", playback_status: "playing" }), cancel: () => false },
    quiet: () => true,
    observe: async (value) => { request = value; return { result: { status: "playing", title: "Song", artist: "Artist", playback_status: "playing", commentary: "一起听吧。", commentary_source: "local" } }; },
  });
  await current.controller.refreshConfig();
  assert.equal(current.timers[0].delay, 0);
  current.controller.stop();
  await current.controller.poll();
  assert.equal(request.source, undefined);
  assert.equal(request.quiet, true);
  assert.match(request.observation_id, /^media:/);
  assert.equal(current.controller.current().updated_at, "2026-07-23T00:00:00.000Z");
  assert.equal(current.commentary.length, 1);
  assert.equal(current.timers.at(-1).delay, 10000);
});

test("unavailable samples back off and recover to the normal interval", async () => {
  let sample = { status: "unavailable" };
  const current = fixture({ readStatus: async () => ({ config: { enabled: true } }), adapter: { sample: async () => sample, cancel: () => false } });
  await current.controller.refreshConfig();
  await current.timers.shift().handler();
  assert.equal(current.timers.at(-1).delay, DELAYS[0]);
  current.controller.stop();
  sample = { status: "empty", title: "", artist: "", playback_status: "closed" };
  await current.controller.poll();
  assert.equal(current.timers.at(-1).delay, 10000);
});

test("disable during observe discards stale metadata and commentary", async () => {
  const observation = deferred();
  let enabled = true, cancelled = 0;
  const current = fixture({
    readStatus: async () => ({ config: { enabled } }),
    adapter: { sample: async () => ({ status: "ready", source: "p", title: "Old", artist: "A", playback_status: "playing" }), cancel: () => { cancelled += 1; return true; } },
    observe: () => observation.promise,
  });
  await current.controller.refreshConfig();
  const poll = current.controller.poll();
  enabled = false;
  await current.controller.refreshConfig();
  observation.resolve({ result: { status: "playing", title: "Old", artist: "A", playback_status: "playing", commentary: "stale", commentary_source: "local" } });
  await poll;
  assert.equal(current.controller.current().status, "disabled");
  assert.equal(current.commentary.length, 0);
  assert.equal(cancelled, 1);
});

test("invalid response fails closed and dispose clears timers", async () => {
  const current = fixture({ readStatus: async () => ({ config: { enabled: true } }), adapter: { sample: async () => ({ status: "empty", title: "", artist: "", playback_status: "closed" }), cancel: () => true }, observe: async () => ({ result: { status: "playing", title: "bad\u0000", artist: "", playback_status: "playing" } }) });
  await current.controller.refreshConfig();
  await current.controller.poll();
  assert.equal(current.controller.current().status, "error");
  current.controller.dispose();
  assert.equal(current.controller.current().status, "disabled");
});
