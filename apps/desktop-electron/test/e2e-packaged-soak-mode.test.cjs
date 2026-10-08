const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const desktopRoot = path.resolve(__dirname, "..");
const script = fs.readFileSync(path.join(desktopRoot, "scripts", "e2e-electron.cjs"), "utf8");
const sampler = fs.readFileSync(path.join(desktopRoot, "scripts", "process-tree-sampler.py"), "utf8");
const packageJson = JSON.parse(fs.readFileSync(path.join(desktopRoot, "package.json"), "utf8"));

test("packaged soak command is opt-in, fixed to the repository candidate, and AppData-isolated", () => {
  assert.equal(
    packageJson.scripts["test:e2e:packaged-soak"],
    "node scripts/e2e-electron.cjs --packaged-soak-only",
  );
  assert.match(script, /PACKAGED_SOAK_ONLY = process\.argv\.includes\("--packaged-soak-only"\)/);
  assert.match(script, /PACKAGED_SOAK_DURATION_SECONDS = 60 \* 60/);
  assert.match(script, /PACKAGED_SOAK_SAMPLE_INTERVAL_SECONDS = 10/);
  assert.match(script, /PACKAGED_SOAK_HEALTH_INTERVAL_SECONDS = 60/);
  assert.match(script, /if \(path\.resolve\(EXE\) !== path\.resolve\(DEFAULT_EXE\)\) throw new Error\("packaged soak Gate requires the repository packaged candidate"\)/);
  assert.match(script, /PACKAGED_SOAK_ONLY \|\| PACKAGED_SOAK_SOURCE_TEST_ONLY/);
  assert.match(script, /if \(PACKAGED_SOAK_ONLY\) \{\s*const result = await runPackagedSoakGate\(temporaryRoot\);/);
});

test("formal soak cannot accept a shortened duration while a nonce-gated source test can", () => {
  assert.match(script, /PACKAGED_SOAK_SOURCE_TEST_NONCE = "p7-packaged-soak-source-test-7a8a7f42"/);
  assert.match(script, /CHRIPTMAS_E2E_PACKAGED_SOAK_SOURCE_TEST_NONCE === PACKAGED_SOAK_SOURCE_TEST_NONCE/);
  assert.match(script, /if \(PACKAGED_SOAK_ONLY && PACKAGED_SOAK_SOURCE_TEST_ONLY\)/);
  assert.match(script, /if \(PACKAGED_SOAK_SOURCE_TEST_ONLY && !PACKAGED_SOAK_SOURCE_TEST_ENABLED\)/);
  assert.match(script, /durationSeconds !== PACKAGED_SOAK_DURATION_SECONDS\s*&& !\(PACKAGED_SOAK_SOURCE_TEST_ENABLED && durationSeconds === 2 && sampleIntervalSeconds === 1 && healthIntervalSeconds === 1\)/);
  assert.match(script, /durationSeconds: 2,\s*sampleIntervalSeconds: 1,\s*healthIntervalSeconds: 1/);
  assert.match(script, /const startedAt = process\.hrtime\.bigint\(\)/);
  assert.match(script, /await sampler\.ready\(\);\s*const startedAt = process\.hrtime\.bigint\(\)/);
});

test("soak is read-only, preserves both process identities, and reports numeric sampler metrics", () => {
  assert.match(script, /assertNoPluginHandsWorkspaces\(temporaryRoot\)/);
  assert.match(script, /readOnlyPackagedSoakHealthPulse\(session\.page\)/);
  assert.match(script, /fetch\(base \+ "\/api\/health"\)/);
  assert.match(script, /fetch\(base \+ "\/api\/rebuild\/library\/overview"\)/);
  assert.ok(script.includes('["#view=home", \'section[aria-label="工作台输入"]\']'));
  assert.ok(script.includes('["#view=rebuild-library-overview", \'main[aria-label="资料库 Overview"]\']'));
  assert.ok(script.includes('["#view=rebuild-settings", \'main[aria-label="Chrip_OS 设置"]\']'));
  assert.match(script, /hasSameWindowsProcessIdentity\(ownerIdentity, readWindowsProcessIdentity\(ownerPid\)\)/);
  assert.match(script, /assertPackagedSoakProcessIdentity\(\{ label: "sidecar", pid: sidecarPid, expectedIdentity: sidecarIdentity, session \}\)/);
  assert.match(script, /packagedSoakSidecarExitEvidence\(session\)/);
  assert.match(script, /child_exit_code=\$\{session\?\.child\?\.exitCode/);
  assert.match(script, /\$\{label\} PID was reused or its process identity changed during packaged soak/);
  assert.match(script, /await closeWorkspaceSessionNormally\(session\)/);
  assert.match(script, /child\.exitCode !== 0 \|\| child\.signalCode !== null/);
  assert.match(script, /minimumSamples: Math\.floor\(durationSeconds \/ sampleIntervalSeconds\)/);
  assert.match(script, /evaluatePackagedSoakEnvelope\(\{/);
  assert.match(script, /resource_envelope: resourceEnvelope/);
  assert.match(script, /expected_minimum_duration_seconds: minimumDurationSeconds/);
  assert.match(script, /access_denied_count: result\.access_denied_count/);
  assert.match(script, /output_redacted: true/);
  assert.match(sampler, /interval_seconds = float\(sys\.argv\[4\]\) if len\(sys\.argv\) > 4 else 0\.1/);
  assert.match(sampler, /"metrics": metrics/);
  assert.match(sampler, /"rss_bytes": rss/);
  assert.match(sampler, /"private_bytes": private_bytes/);
  assert.match(sampler, /"handles": handles/);
  assert.match(sampler, /"threads": threads/);
  assert.match(sampler, /"access_denied_count": access_denied_count/);
  assert.match(sampler, /root_create_time = root\.create_time\(\)/);
  assert.match(sampler, /ready_path\.write_text\("ready\\n", encoding="utf-8"\)/);
  assert.match(sampler, /next_sample_at \+= interval_seconds/);
  assert.match(sampler, /sleep_seconds = next_sample_at - time\.monotonic\(\)/);
  assert.match(sampler, /process_tree_sampler_root_identity_changed/);
  assert.match(sampler, /"read_bytes": current_read/);
  assert.match(sampler, /"write_bytes": current_write/);
});
