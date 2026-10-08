const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const desktopRoot = path.resolve(__dirname, "..");
const script = fs.readFileSync(path.join(desktopRoot, "scripts", "e2e-electron.cjs"), "utf8");
const packageJson = JSON.parse(fs.readFileSync(path.join(desktopRoot, "package.json"), "utf8"));

test("AI Turn owner-kill recovery gate stays opt-in and uses a direct owner kill", () => {
  assert.equal(
    packageJson.scripts["test:e2e:ai-turn-kill-recovery"],
    "node scripts/e2e-electron.cjs --ai-turn-kill-recovery-only",
  );
  assert.match(script, /AI_TURN_KILL_RECOVERY_ONLY = process\.argv\.includes\("--ai-turn-kill-recovery-only"\)/);
  assert.match(script, /if \(AI_TURN_KILL_RECOVERY_ONLY\) \{\s*const result = await runPackagedAiTurnKillRecoveryGate\(temporaryRoot\)/);
  assert.match(script, /resolveAiTurnKillRecoveryCandidate\(\{/);
  assert.match(script, /exe: E2E_EXE_INPUT/);
  assert.match(script, /candidateId: AI_TURN_KILL_CANDIDATE_ID/);
  assert.match(script, /sourceCommit: AI_TURN_KILL_SOURCE_COMMIT/);
  assert.match(script, /packaged candidate does not contain the parent-liveness contract/);
  assert.match(script, /spawnSync\("taskkill\.exe", \["\/pid", String\(runningSession\.child\.pid\), "\/f"\]/);
  assert.doesNotMatch(script, /runningSession\.child\.pid\), "\/t"/i);
});

test("AI Turn owner-kill recovery gate only reaches its blocked provider after approval", () => {
  assert.match(script, /window\.__p7AiTurnKillTestLabPromise = fetch/);
  assert.match(script, /'\/api\/rebuild\/developer-studio\/test-lab'/);
  assert.match(script, /provider_call_confirmed: true/);
  assert.match(script, /inspectAiTurnKillPreWireFailure\(runningSession\.page\)/);
  assert.match(script, /event_type: item\?\.event_type, error_code: item\?\.error_code/);
  assert.match(script, /effect_certainty: attempt\?\.effect_certainty/);
  assert.match(script, /inspectAiTurnKillRecoveryAuthority\(temporaryRoot, diagnostics\.response\.turn_id\)/);
  assert.match(script, /inspectAiTurnKillRecoveryAuthority\(temporaryRoot, null\)/);
  assert.match(script, /item\.get\('type'\)=='model\.attempt\.dispatched'/);
  assert.match(script, /wire\?\.method !== "POST" \|\| wire\?\.path !== "\/v1\/chat\/completions"/);
  assert.match(script, /Date\.parse\(preKillAuthority\.lease_stale_after\)/);
  assert.match(script, /recovered\.events_after_cursor\.length !== 0/);
  assert.match(script, /redact\(error\?\.stack \|\| error\?\.message \|\| error\)/);
  assert.match(script, /reason_code === 'ai\.recovery_model_incomplete'/);
});
