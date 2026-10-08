const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const [repoRoot, appData, readyPath, resultPath] = process.argv.slice(2).map((value) => path.resolve(value));
const resources = path.join(repoRoot, "apps", "desktop-electron", "release", "win-unpacked", "resources");
const sidecarRoot = path.join(resources, "sidecar");
const supervisorPath = path.join(resources, "app", "src", "sidecar-supervisor.cjs");

function writeJson(target, value) {
  const pending = `${target}.pending`;
  fs.writeFileSync(pending, JSON.stringify(value), "utf8");
  fs.renameSync(pending, target);
}

function requireFreshCandidate() {
  const required = [
    [path.join(sidecarRoot, "backend", "api", "plugin_hook_runtime.py"), "set_handler_prefix_enabled"],
    [path.join(sidecarRoot, "rebuild", "plugin_host", "package_intake.py"), "_CODEX_HOOK_EVENTS = frozenset({\"PreToolUse\"})"],
    [path.join(sidecarRoot, "rebuild", "ai_kernel", "codex_hook_runtime.py"), "_disabled_handler_prefixes"],
  ];
  for (const [file, marker] of required) {
    if (!fs.statSync(file, { throwIfNoEntry: false })?.isFile() || !fs.readFileSync(file, "utf8").includes(marker)) {
      throw new Error(`packaged_candidate_stale:${path.relative(resources, file)}:${marker}`);
    }
  }
}

function packageSource(root) {
  const plugin = path.join(root, ".rebuild-data", "plugin-package-inbox", "packaged-hook-plugin");
  const hand = path.join(plugin, "hands", "policy-hand");
  const payload = path.join(hand, "payload");
  const hook = path.join(plugin, "hooks", "pre-tool-policy");
  fs.mkdirSync(path.join(plugin, ".codex-plugin"), { recursive: true });
  fs.mkdirSync(payload, { recursive: true });
  fs.mkdirSync(hook, { recursive: true });
  fs.writeFileSync(path.join(plugin, ".codex-plugin", "plugin.json"), JSON.stringify({ name: "packaged-hook-plugin", version: "1.0.0", description: "packaged Hook live Gate" }));
  const inputSchema = { type: "object", properties: { hook_event: { type: "string" }, payload: { type: "object" } }, required: ["hook_event", "payload"], additionalProperties: false };
  const outputSchema = { type: "object", properties: { exit_code: { type: "integer" }, stdout: { type: "string" }, stderr: { type: "string" } }, required: ["exit_code", "stdout", "stderr"], additionalProperties: false };
  fs.writeFileSync(path.join(hand, "hand.json"), JSON.stringify({ schema_version: "1.0.0", id: "policy-hand", runtime: "powershell-stdio-v1", entrypoint: "payload/main.ps1", input_schema: inputSchema, output_schema: outputSchema, effect: "read", operation_semantics: "read_only", requested_resources: [] }));
  fs.writeFileSync(path.join(hook, "hook.json"), JSON.stringify({ schema_version: "1.0.0", id: "pre-tool-policy", hand_id: "policy-hand", event: "PreToolUse", order: 10, sync: true, timeout_ms: 10000, metadata_projection: "codex-hook-v1", recursion: "deny" }));
  const denied = JSON.stringify({ hookSpecificOutput: { hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: "packaged contained policy" } }).replaceAll("'", "''");
  const source = [
    "$hello = @{protocol='plugin-hands/1';type='hello';launch_id=$env:CHRIPTMAS_PLUGIN_HANDS_LAUNCH_ID;lease_id=$env:CHRIPTMAS_PLUGIN_HANDS_LEASE_ID;invocation_id=$env:CHRIPTMAS_PLUGIN_HANDS_INVOCATION_ID}",
    "[Console]::Out.WriteLine(($hello | ConvertTo-Json -Compress))",
    "$request = [Console]::In.ReadLine() | ConvertFrom-Json",
    "if ($null -ne $request.input.payload.turn_id) { Start-Sleep -Seconds 3; $stdout='" + denied + "' } else { $stdout='' }",
    "$result = @{protocol='plugin-hands/1';type='result';launch_id=$env:CHRIPTMAS_PLUGIN_HANDS_LAUNCH_ID;lease_id=$env:CHRIPTMAS_PLUGIN_HANDS_LEASE_ID;invocation_id=$env:CHRIPTMAS_PLUGIN_HANDS_INVOCATION_ID;output=@{exit_code=0;stdout=$stdout;stderr=''}}",
    "[Console]::Out.WriteLine(($result | ConvertTo-Json -Depth 8 -Compress))",
  ].join("\n");
  fs.writeFileSync(path.join(payload, "main.ps1"), source, "utf8");
  return plugin;
}

async function call(origin, secret, method, route, body, expected = null) {
  const response = await fetch(`${origin}${route}`, { method, headers: { "Content-Type": "application/json", "X-Chriptmas-Desktop-Session": secret }, body: body === undefined ? undefined : JSON.stringify(body) });
  const payload = await response.json();
  if ((expected === null && !response.ok) || (expected !== null && response.status !== expected)) throw new Error(`http_${response.status}:${route}:${JSON.stringify(payload)}`);
  return payload;
}

async function waitForTurn(origin, secret, turnId, terminalType) {
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    const events = await call(origin, secret, "GET", `/api/ai/turns/${turnId}/events`);
    if (events.events.some((item) => item.type === terminalType)) return events.events;
    if (events.events.some((item) => ["turn.failed", "turn.cancelled"].includes(item.type)) && terminalType !== "turn.failed") throw new Error(`turn_failed:${turnId}:${JSON.stringify(events.events.at(-1))}`);
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`turn_timeout:${turnId}:${terminalType}`);
}

async function submit(origin, secret, turnId, capabilityId) {
  return call(origin, secret, "POST", "/api/ai/turns", { schema_version: "1.0.0", turn_id: turnId, session_id: "packaged-hook-session", operation_id: `op-${turnId}`, idempotency_key: `idem-${turnId}`, scope: { kind: "project", project_id: "packaged-hook-project", series_id: null }, input: { kind: "text", text: "execute governed policy hand", refs: [] }, desired_outcome: capabilityId, privacy: { mode: "local_only", allow_remote: false, pii: "none", consent_refs: [], retention: "local_durable" }, capability_policy: { allowed: [capabilityId], denied: [], require_approval: [] }, context_policy: { include_project_skill: false, include_memory: false, include_session_history: false, max_context_bytes: 1024 }, approval_policy: { mode: "risk_based", auto_approve_read_only: true }, capability_request: { mode: "execute_exact_v1", capability_id: capabilityId, arguments: { hook_event: "fixture", payload: { mode: "tool" } } }, created_at: "2026-08-27T00:00:00Z" });
}

function sqliteJsonRows(pythonPath, database, sql, parameters) {
  const source = "import json,pathlib,sqlite3,sys; u=pathlib.Path(sys.argv[1]).resolve().as_uri()+'?mode=ro'; c=sqlite3.connect(u,uri=True); rows=c.execute(sys.argv[2],json.loads(sys.argv[3])).fetchall(); print(json.dumps([json.loads(r[0]) for r in rows],separators=(',',':')))";
  const result = spawnSync(pythonPath, ["-I", "-c", source, database, sql, JSON.stringify(parameters)], { encoding: "utf8", windowsHide: true });
  if (result.status !== 0) throw new Error(`sqlite_fixture_read_failed:${String(result.stderr).slice(-1000)}`);
  return JSON.parse(result.stdout);
}

async function main() {
  requireFreshCandidate();
  const { SidecarSupervisor } = require(supervisorPath);
  const pythonPath = path.join(sidecarRoot, "runtime", "python.exe");
  fs.mkdirSync(path.join(appData, "config"), { recursive: true });
  fs.copyFileSync(path.join(sidecarRoot, "config", "settings.toml"), path.join(appData, "config", "settings.toml"));
  const packagePath = packageSource(appData);
  let supervisor;
  try {
    supervisor = new SidecarSupervisor({ rootDir: appData, moduleRoot: sidecarRoot, workingDir: appData, pythonPath, startupTimeoutMs: 45000 });
    let session = await supervisor.start();
    const discovered = await call(session.origin, session.secret, "POST", "/api/ai/governance/plugins/packages/discover", { source_path: packagePath, command_id: "discover-packaged-hook-0001" });
    const installed = await call(session.origin, session.secret, "POST", "/api/ai/governance/plugins/packages/packaged-hook-plugin/install-disabled", { expected_state_revision: discovered.state_revision, command_id: "install-packaged-hook-0001", confirm: true });
    const handReview = await call(session.origin, session.secret, "POST", "/api/ai/governance/plugins/packages/packaged-hook-plugin/hands/policy-hand/review", { expected_state_revision: installed.state_revision, command_id: "review-packaged-hook-hand-0001", confirm: true, reason: "packaged Hook Gate" });
    const materialized = await call(session.origin, session.secret, "POST", "/api/ai/governance/plugins/packages/packaged-hook-plugin/hands/policy-hand/materialize", { expected_review_revision: handReview.review_revision, expected_materialization_revision: 0, command_id: "materialize-packaged-hook-hand-0001", confirm: true });
    const handActive = await call(session.origin, session.secret, "POST", "/api/ai/governance/plugins/packages/packaged-hook-plugin/hands/policy-hand/activate", { expected_review_revision: handReview.review_revision, expected_materialization_revision: materialized.materialization_revision, expected_activation_revision: 0, command_id: "activate-packaged-hook-hand-0001", confirm: true });
    await call(session.origin, session.secret, "POST", "/api/ai/governance/plugins/packages/packaged-hook-plugin/hands/policy-hand/projects/packaged-hook-project/enable", { expected_profile_revision: 0, confirm: true });
    const hookReview = await call(session.origin, session.secret, "POST", "/api/ai/governance/plugins/packages/packaged-hook-plugin/hooks/pre-tool-policy/review", { expected_state_revision: installed.state_revision, expected_hand_activation_revision: handActive.activation_revision, command_id: "review-packaged-hook-0001", confirm: true, reason: "direct packaged deny" });
    const hookActive = await call(session.origin, session.secret, "POST", "/api/ai/governance/plugins/packages/packaged-hook-plugin/hooks/pre-tool-policy/activate", { expected_review_revision: hookReview.review_revision, expected_activation_revision: 0, command_id: "activate-packaged-hook-0001", confirm: true });
    writeJson(readyPath, { sidecar_pid: session.child_pid });
    const capabilityId = "plugin.hand.packaged-hook-plugin.policy-hand";
    const deniedTurn = "turn-11111111111111111111111111111111";
    await submit(session.origin, session.secret, deniedTurn, capabilityId);
    const deniedEvents = await waitForTurn(session.origin, session.secret, deniedTurn, "turn.failed");
    if (!deniedEvents.some((item) => item.type === "hook.invoked")) throw new Error("hook_receipt_missing");
    if (deniedEvents.some((item) => item.type === "tool.started")) throw new Error("denied_tool_started");
    const receipts = sqliteJsonRows(pythonPath, path.join(appData, ".rebuild-data", "ai-turns.sqlite3"), "SELECT payload_json FROM ai_turn_payloads WHERE turn_id=? AND kind='codex-hook-invocation-receipt'", [deniedTurn]);
    if (receipts.length !== 1) throw new Error(`hook_receipt_count:${receipts.length}`);
    const hookReceipt = receipts[0];
    const expectedHandler = "plugin.packaged-hook-plugin.pre-tool-policy";
    if (hookReceipt.normalized_outcome?.dispatch_blocked !== true || hookReceipt.normalized_outcome?.reason !== "packaged contained policy") throw new Error(`hook_deny_not_proven:${JSON.stringify(hookReceipt.normalized_outcome)}`);
    if (hookReceipt.handler_runs?.length !== 1 || hookReceipt.handler_runs[0].handler_id !== expectedHandler || hookReceipt.handler_runs[0].status !== "blocked") throw new Error(`hook_handler_not_blocked:${JSON.stringify(hookReceipt.handler_runs)}`);
    const privateLifecyclePath = path.join(appData, ".rebuild-data", "plugin-hook-lifecycles.sqlite3");
    if (fs.existsSync(privateLifecyclePath)) throw new Error("private_hook_lifecycle_present");
    const disabled = await call(session.origin, session.secret, "POST", "/api/ai/governance/plugins/packages/packaged-hook-plugin/hooks/pre-tool-policy/disable", { expected_activation_revision: hookActive.activation_revision, command_id: "disable-packaged-hook-0001", reason: "prove immediate revoke" });
    const completedTurn = "turn-22222222222222222222222222222222";
    await submit(session.origin, session.secret, completedTurn, capabilityId);
    await waitForTurn(session.origin, session.secret, completedTurn, "tool.completed");
    await supervisor.stop(); supervisor = null;
    supervisor = new SidecarSupervisor({ rootDir: appData, moduleRoot: sidecarRoot, workingDir: appData, pythonPath, startupTimeoutMs: 45000 });
    session = await supervisor.start();
    const restartedTurn = "turn-33333333333333333333333333333333";
    await submit(session.origin, session.secret, restartedTurn, capabilityId);
    await waitForTurn(session.origin, session.secret, restartedTurn, "tool.completed");
    writeJson(resultPath, { status: "ok", sidecar_pid: session.child_pid, denied_hook_receipts: receipts.length, denied_handler: hookReceipt.handler_runs[0].handler_id, private_lifecycle_absent: true, disabled_revision: disabled.activation_revision });
  } finally {
    if (supervisor) await supervisor.stop().catch(() => {});
  }
}

main().catch((error) => { writeJson(resultPath, { status: "failed", error: String(error.stack || error) }); process.exitCode = 1; });
