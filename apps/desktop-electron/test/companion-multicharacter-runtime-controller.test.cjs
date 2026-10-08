const assert = require("node:assert/strict");
const test = require("node:test");

const {
  ACTION_MESSAGES,
  CompanionMultiCharacterRuntimeController,
  UNAVAILABLE_PROJECTION,
} = require("../src/companion/multicharacter-runtime-controller.cjs");

function settingsValue(overrides = {}) {
  return Object.freeze({
    schema_version: 1,
    enabled: false,
    consented: false,
    character_id: "chriptmas.bear",
    allowed_character_ids: Object.freeze([]),
    revision: 0,
    ...overrides,
  });
}

function fixture(overrides = {}) {
  const calls = [];
  let current = settingsValue();
  const settings = {
    read: () => current,
    save: (payload) => {
      calls.push(["save", payload]);
      current = settingsValue({
        enabled: payload.enabled,
        consented: payload.consented,
        character_id: payload.characterId,
        allowed_character_ids: Object.freeze([...payload.allowedCharacterIds]),
        revision: current.revision + 1,
      });
      return current;
    },
  };
  const links = [];
  const createLink = (value) => {
    const link = {
      value,
      cleanup: () => calls.push(["cleanup", value.character_id]),
      start: async () => calls.push(["start", value.character_id]),
      stop: async () => calls.push(["stop", value.character_id]),
      peers: () => [],
      send: async (instanceId, message) => {
        calls.push(["send", instanceId, message]);
        return { status: "accepted", token: "must-not-project" };
      },
    };
    links.push(link);
    return link;
  };
  const controller = new CompanionMultiCharacterRuntimeController({
    createSettings: () => settings,
    createLink,
    ...overrides,
  });
  return { controller, calls, settings, links, setSettings: (value) => { current = value; } };
}

test("is unavailable before initialization and cleans stale discovery while disabled", async () => {
  const value = fixture();
  assert.deepEqual(value.controller.projection(), UNAVAILABLE_PROJECTION);
  assert.equal(Object.isFrozen(value.controller.projection()), true);
  assert.deepEqual(await value.controller.initialize(), {
    state: "ready", ...settingsValue(), peers: [],
  });
  assert.deepEqual(value.calls, [["cleanup", "chriptmas.bear"]]);
  assert.equal(value.controller.link, null);
});

test("enabled initialization starts one link and projects only public peer facts", async () => {
  const value = fixture();
  value.setSettings(settingsValue({
    enabled: true,
    consented: true,
    allowed_character_ids: Object.freeze(["friend.cat"]),
    revision: 2,
  }));
  await value.controller.initialize();
  value.controller.link.peers = () => [{
    instance_id: "a".repeat(36),
    character_id: "friend.cat",
    state: "idle",
    protocol_version: 1,
    port: 43210,
    token: "secret",
  }];
  const projection = value.controller.projection();
  assert.deepEqual(projection.peers, [{ instance_id: "a".repeat(36), character_id: "friend.cat", state: "idle", compatible: true }]);
  assert.equal(JSON.stringify(projection).includes("secret"), false);
  assert.equal(JSON.stringify(projection).includes("43210"), false);
  assert.equal(Object.isFrozen(projection.peers), true);
});

test("configuration delegates revision and consent facts then replaces the owned link", async () => {
  const value = fixture();
  value.setSettings(settingsValue({ enabled: true, consented: true }));
  await value.controller.initialize();
  const result = await value.controller.configure({
    enabled: true,
    consented: true,
    character_id: "chriptmas.fox",
    allowed_character_ids: ["friend.cat"],
    expected_revision: 0,
  });
  assert.equal(result.character_id, "chriptmas.fox");
  assert.deepEqual(value.calls.map(([name]) => name), ["start", "save", "stop", "start"]);
  assert.deepEqual(value.calls[1][1], {
    enabled: true,
    consented: true,
    characterId: "chriptmas.fox",
    allowedCharacterIds: ["friend.cat"],
    expectedRevision: 0,
  });
});

test("failed replacement cleans the new link and never retains a half-started owner", async () => {
  const calls = [];
  const settings = settingsValue({ enabled: true, consented: true });
  const controller = new CompanionMultiCharacterRuntimeController({
    createSettings: () => ({ read: () => settings, save: () => settings }),
    createLink: () => ({
      cleanup: () => {},
      start: async () => { calls.push("start"); throw new Error("bind_failed"); },
      stop: async () => calls.push("stop"),
      peers: () => [],
    }),
  });
  await assert.rejects(controller.initialize(), /bind_failed/);
  assert.deepEqual(calls, ["start", "stop"]);
  assert.equal(controller.link, null);
});

test("fixed actions map to closed messages and return only peer status", async () => {
  const value = fixture();
  value.setSettings(settingsValue({ enabled: true, consented: true }));
  await value.controller.initialize();
  for (const action of Object.keys(ACTION_MESSAGES)) {
    assert.deepEqual(await value.controller.sendAction("peer-id", action), { status: "accepted" });
  }
  assert.deepEqual(value.calls.filter(([name]) => name === "send").map(([, , message]) => message), Object.values(ACTION_MESSAGES));
  await assert.rejects(value.controller.sendAction("peer-id", "command"), /companion_multicharacter_payload_rejected/);
});

test("uninitialized send and repeated stop remain bounded", async () => {
  const value = fixture();
  await assert.rejects(value.controller.sendAction("peer-id", "wave"), /companion_multicharacter_unavailable/);
  await value.controller.stop();
  value.setSettings(settingsValue({ enabled: true, consented: true }));
  await value.controller.initialize();
  await value.controller.stop();
  await value.controller.stop();
  assert.equal(value.calls.filter(([name]) => name === "stop").length, 1);
});
