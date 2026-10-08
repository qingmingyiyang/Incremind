const assert = require("node:assert/strict");
const test = require("node:test");

const { recordCompanionInteraction } = require("../src/companion/interaction-recorder.cjs");

const BASE = {
  origin: "http://127.0.0.1:43123",
  secret: "session-secret",
  sessionHeader: "X-Chriptmas-Session",
  eventId: "gesture:petting:abc123",
  timeoutSignal: () => "bounded-signal",
  wait: async () => {},
};

test("interaction retry reuses the same event ID and fixed petting body", async () => {
  const requests = [];
  const result = await recordCompanionInteraction({
    ...BASE,
    fetchFn: async (_url, options) => {
      requests.push(options);
      return requests.length === 1 ? { ok: false, status: 503 } : { ok: true, status: 200 };
    },
  });
  assert.deepEqual(result, { status: "recorded", replayed: true });
  assert.equal(requests.length, 2);
  assert.equal(requests[0].body, requests[1].body);
  assert.deepEqual(JSON.parse(requests[0].body), { event_id: BASE.eventId, kind: "petting" });
  assert.equal(requests[0].signal, "bounded-signal");
  assert.equal("affinity" in JSON.parse(requests[0].body), false);
});

test("interaction recorder does not retry rejected requests or accept unsafe facts", async () => {
  let calls = 0;
  const rejected = await recordCompanionInteraction({
    ...BASE,
    fetchFn: async () => { calls += 1; return { ok: false, status: 400 }; },
  });
  assert.deepEqual(rejected, { status: "rejected" });
  assert.equal(calls, 1);
  await assert.rejects(() => recordCompanionInteraction({ ...BASE, eventId: "bad event" }), /event_id_invalid/);
  await assert.rejects(() => recordCompanionInteraction({ ...BASE, origin: "https://example.com" }), /origin_invalid/);
});
