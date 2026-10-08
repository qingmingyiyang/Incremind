const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const desktopRoot = path.resolve(__dirname, "..");
const script = fs.readFileSync(path.join(desktopRoot, "scripts", "e2e-electron.cjs"), "utf8");
const packageJson = JSON.parse(fs.readFileSync(path.join(desktopRoot, "package.json"), "utf8"));

test("renderer crash cursor gate stays opt-in, packaged, and AppData-isolated", () => {
  assert.equal(
    packageJson.scripts["test:e2e:renderer-crash-cursor"],
    "node scripts/e2e-electron.cjs --renderer-crash-cursor-only",
  );
  assert.match(script, /RENDERER_CRASH_CURSOR_ONLY = process\.argv\.includes\("--renderer-crash-cursor-only"\)/);
  assert.match(script, /if \(RENDERER_CRASH_CURSOR_ONLY\) \{\s*const result = await runPackagedRendererCrashCursorGate\(temporaryRoot\)/);
  assert.match(script, /path\.resolve\(EXE\) !== path\.resolve\(DEFAULT_EXE\)/);
  assert.match(script, /packaged candidate does not contain the renderer crash recovery controller/);
  assert.match(script, /COMPANION_MULTICHARACTER_ONLY \|\| AI_TURN_KILL_RECOVERY_ONLY \|\| RENDERER_CRASH_CURSOR_ONLY/);
  assert.match(script, /renderer-crash-cursor-recovery\.json/);
});

test("renderer crash cursor gate uses the generic durable Turn API and checks cursor exactness", () => {
  assert.match(script, /desired_outcome: "companion\.chat\.respond"/);
  assert.match(script, /fetch\(base \+ "\/api\/ai\/turns"/);
  assert.match(script, /approval\.type !== "approval\.required"/);
  assert.match(script, /await session\.page\.send\("Page\.crash", \{\}, 5000\)/);
  assert.match(script, /Target crashed\|WebSocket closed\|connection failed\|timed out/);
  assert.match(script, /reconnectMainRenderer\(session, \{ previousRendererProbe: true, expectedTargetId: originalTargetId \}\)/);
  assert.match(script, /renderer JavaScript context did not reload after crash/);
  assert.match(script, /reloadedTimeOrigin === originalTimeOrigin/);
  assert.match(script, /JSON\.stringify\(completeEvents\) !== beforeEventsJson/);
  assert.match(script, /projection\?\.status !== "waiting_approval"/);
  assert.match(script, /hasSameWindowsProcessIdentity\(ownerIdentity, readWindowsProcessIdentity\(ownerPid\)\)/);
  assert.match(script, /hasSameWindowsProcessIdentity\(sidecarIdentity, readWindowsProcessIdentity\(sidecarPid\)\)/);
  assert.match(script, /readRendererCrashCursorTurn\(session\.page, turnId, approvalCursor\)/);
  assert.match(script, /readRendererCrashCursorTurn\(session\.page, turnId, approvalCursor - 1\)/);
  assert.match(script, /renderer recovery did not replay the exact durable approval event from cursor minus one/);
  assert.match(script, /renderer recovery changed the durable AI Turn event history/);
});
