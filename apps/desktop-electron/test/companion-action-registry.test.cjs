const assert = require("node:assert/strict");
const test = require("node:test");

const { CompanionActionRegistry, validatePanelPayload } = require("../src/companion/action-registry.cjs");

test("action registry owns ordering, separators, sources and execution", async () => {
  const calls = [];
  const registry = new CompanionActionRegistry([
    { id: "pet.first", label: "第一项", sources: ["pet"], menu: { group: 0, order: 10 }, handler: async () => calls.push("first") },
    { id: "pet.disabled", label: "禁用项", sources: ["pet"], menu: { group: 1, order: 20 }, enabled: false, handler: async () => calls.push("disabled") },
  ]);
  const template = registry.menuTemplate({ source: "pet" });
  assert.deepEqual(template.map((item) => item.type || item.id), ["pet.first", "separator", "pet.disabled"]);
  assert.equal(template[2].enabled, false);
  await template[0].click();
  assert.deepEqual(calls, ["first"]);
  assert.deepEqual(await registry.execute("pet.disabled", { source: "pet" }), { status: "disabled", action_id: "pet.disabled" });
  await assert.rejects(() => registry.execute("pet.first", { source: "tray" }), /source_rejected/);
  await assert.rejects(() => registry.execute("pet.first", { source: "pet", payload: { template: [] } }), /payload_rejected/);
});

test("panel payload is an allowlisted identifier only", () => {
  assert.deepEqual(validatePanelPayload({ panel: "focus" }), { panel: "focus" });
  assert.throws(() => validatePanelPayload({ panel: "../../secret" }), /payload_rejected/);
  assert.throws(() => validatePanelPayload({ panel: "chat", url: "https://example.com" }), /payload_rejected/);
});
