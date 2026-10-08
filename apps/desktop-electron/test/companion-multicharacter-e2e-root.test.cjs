const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  TEST_DIRECTORY,
  TEST_ENV,
  TEST_SWITCH,
  resolveMultiCharacterE2ERoot,
} = require("../src/companion/multicharacter-e2e-root.cjs");

function fixture(participant = "participant-a") {
  const temporary = os.tmpdir();
  const runRoot = path.join(temporary, "chriptmas-multicharacter-fixture");
  return {
    argv: [TEST_SWITCH],
    env: { [TEST_ENV]: "a".repeat(32) },
    tempRoot: temporary,
    userDataRoot: path.join(runRoot, participant),
  };
}

test("dual-gated participants derive one shared temporary discovery root", () => {
  const first = fixture("participant-a");
  const second = fixture("participant-b");
  const expected = path.join(first.tempRoot, TEST_DIRECTORY, "a".repeat(32));
  assert.equal(resolveMultiCharacterE2ERoot(first), expected);
  assert.equal(resolveMultiCharacterE2ERoot(second), expected);
});

test("missing either E2E gate fails closed", () => {
  const value = fixture();
  assert.equal(resolveMultiCharacterE2ERoot({ ...value, argv: [] }), null);
  assert.equal(resolveMultiCharacterE2ERoot({ ...value, env: {} }), null);
  assert.equal(resolveMultiCharacterE2ERoot({ ...value, env: { [TEST_ENV]: "1" } }), null);
});

test("relative, Temp root itself and formal userData paths fail closed", () => {
  const value = fixture();
  assert.equal(resolveMultiCharacterE2ERoot({ ...value, userDataRoot: "relative" }), null);
  assert.equal(resolveMultiCharacterE2ERoot({ ...value, userDataRoot: value.tempRoot }), null);
  assert.equal(resolveMultiCharacterE2ERoot({
    ...value,
    userDataRoot: path.join(path.parse(value.tempRoot).root, "Users", "Formal", "AppData", "Roaming", "Chriptmas OS"),
  }), null);
});

test("main uses one resolved root for disabled cleanup and enabled startup", () => {
  const main = fs.readFileSync(path.join(__dirname, "..", "src", "main.cjs"), "utf8");
  const runtime = fs.readFileSync(path.join(__dirname, "..", "src", "companion", "multicharacter-runtime-controller.cjs"), "utf8");
  assert.match(main, /function multiCharacterDiscoveryRoot\(\)[\s\S]*resolveMultiCharacterE2ERoot\(\{ userDataRoot: app\.getPath\("userData"\) \}\)[\s\S]*app\.getPath\("appData"\)/);
  assert.match(main, /createLink: \(settings\) => new CompanionMultiCharacterLink\(\{[\s\S]*root: multiCharacterDiscoveryRoot\(\)/);
  assert.match(runtime, /if \(settings\.enabled\) await this\.replaceLink\(settings\);[\s\S]*else this\.createLink\(settings\)\.cleanup\(\)/);
  assert.match(runtime, /const next = this\.createLink\(settings\)/);
  assert.equal((main.match(/companion-network/g) || []).length, 1);
});
