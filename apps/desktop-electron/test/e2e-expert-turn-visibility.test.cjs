const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.resolve(__dirname, "..");
const script = fs.readFileSync(path.join(root, "scripts", "e2e-electron.cjs"), "utf8");

test("expert Turn packaged gate stays opt-in and uses the normal workbench input", () => {
  assert.match(script, /EXPERT_TURN_VISIBILITY_ONLY = process\.argv\.includes\("--expert-turn-visibility-only"\)/);
  assert.match(script, /if \(EXPERT_TURN_VISIBILITY_ONLY\) \{\s*const result = await runPackagedExpertTurnVisibilityGate\(temporaryRoot\)/);
  assert.match(script, /section\[aria-label="工作台输入"\] textarea/);
  assert.match(script, /button\[aria-label="记住"\]/);
  assert.match(script, /\[aria-label="当前专家"\] dd/);
  assert.match(script, /selected_expert !== 'workbench-question-expert'/);
  assert.match(script, /createExpertTurnVisibilityProviderFixture/);
  assert.match(script, /CHRIPTMAS_E2E_EXPERT_TURN_PROVIDER_ORIGIN: providerFixture\.origin/);
  assert.match(script, /providerFixture\.requests\.length !== 1/);
  assert.match(script, /memory_publication_state !== 'not_published'/);
});

test("expert Turn fixture remains tied to the existing Electron plus sidecar nonce gates", () => {
  assert.match(script, /--mixed-media-e2e-fixture=\$\{token\}/);
  assert.match(script, /CHRIPTMAS_E2E_MIXED_MEDIA_RUN_TOKEN: token/);
  assert.match(script, /CHRIPTMAS_E2E_EXPERT_TURN_RUN_TOKEN: token/);
  assert.match(script, /fixed_packaged_candidate: true/);
  assert.match(script, /restart_projection_exact:true/);
});

test("integrated candidate review follows the explicit memory-candidate label", () => {
  assert.match(script, /kind === '记忆候选'/);
  assert.match(script, /aria-label="记忆候选详情"/);
  assert.doesNotMatch(script, /kind === '记忆' && \(!\$\{encodedTitle\}/);
});

test("integrated question capture follows the durable AI Turn projection", () => {
  assert.match(script, /\/api\/ai\/turns\/.*\/events\?view=simple/);
  assert.match(script, /body: snapshot\.presentation/);
});

test("direct question gate preserves the current honest L0 intake wording", () => {
  assert.match(script, /result\.report\.includes\('原始资料已保存'\)/);
  assert.match(script, /result\.report\.includes\('已自动整理到你的记忆里'\)/);
});
