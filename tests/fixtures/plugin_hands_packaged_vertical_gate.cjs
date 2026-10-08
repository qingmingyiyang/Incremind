const fs = require("node:fs");
const path = require("node:path");

const [repoRoot, appData, readyPath, stopPath, resultPath] = process.argv.slice(2).map((value) => path.resolve(value));
const resources = path.join(repoRoot, "apps", "desktop-electron", "release", "win-unpacked", "resources");
const sidecarRoot = path.join(resources, "sidecar");
const appRoot = path.join(resources, "app");
const supervisorPath = path.join(appRoot, "src", "sidecar-supervisor.cjs");

function writeJson(target, value) {
  const pending = `${target}.pending`;
  fs.writeFileSync(pending, JSON.stringify(value), "utf8");
  fs.renameSync(pending, target);
}

function requireFreshCandidate() {
  const required = [
    [path.join(sidecarRoot, "rebuild", "ai_kernel", "runtime.py"), "_run_exact_capability_request"],
    [path.join(sidecarRoot, "rebuild", "plugin_hands", "contracts.py"), "CHRIPTMAS_PLUGIN_HANDS_LAUNCH_ID"],
    [path.join(sidecarRoot, "backend", "api", "plugin_hands_runtime.py"), "PLUGIN_HANDS_RESOURCE_POLICY_REVISION"],
    [path.join(sidecarRoot, "backend", "api", "plugin_hands_lifecycle_projection.py"), "PluginHandsLifecycleAuditProjection"],
    [path.join(sidecarRoot, "config", "codex-hooks.toml"), "[hooks]"],
  ];
  for (const [file, marker] of required) {
    if (!fs.statSync(file, { throwIfNoEntry: false })?.isFile() || !fs.readFileSync(file, "utf8").includes(marker)) {
      throw new Error(`packaged_candidate_stale:${path.relative(resources, file)}:${marker}`);
    }
  }
}

function packageSource(root) {
  const plugin = path.join(root, ".rebuild-data", "plugin-package-inbox", "packaged-hand-plugin");
  const payload = path.join(plugin, "hands", "long-running", "payload");
  fs.mkdirSync(path.join(plugin, ".codex-plugin"), { recursive: true });
  fs.mkdirSync(payload, { recursive: true });
  fs.writeFileSync(path.join(plugin, ".codex-plugin", "plugin.json"), JSON.stringify({ name: "packaged-hand-plugin", version: "1.0.0", description: "packaged vertical gate" }));
  fs.writeFileSync(path.join(plugin, "hands", "long-running", "hand.json"), JSON.stringify({
    schema_version: "1.0.0", id: "long-running", runtime: "powershell-stdio-v1", entrypoint: "payload/main.ps1",
    input_schema: { type: "object", properties: { value: { type: "string" } }, required: ["value"], additionalProperties: false },
    output_schema: { type: "object", properties: { value: { type: "string" } }, required: ["value"], additionalProperties: false },
    effect: "write", operation_semantics: "receipt_required", requested_resources: ["workspace_output"],
  }));
  const source = [
    "$hello = @{protocol='plugin-hands/1';type='hello';launch_id=$env:CHRIPTMAS_PLUGIN_HANDS_LAUNCH_ID;lease_id=$env:CHRIPTMAS_PLUGIN_HANDS_LEASE_ID;invocation_id=$env:CHRIPTMAS_PLUGIN_HANDS_INVOCATION_ID}",
    "[Console]::Out.WriteLine(($hello | ConvertTo-Json -Compress))",
    "$invoke = [Console]::In.ReadLine()",
    "if ([string]::IsNullOrWhiteSpace($invoke)) { exit 2 }",
    "$request = $invoke | ConvertFrom-Json",
    "if ($request.input.value -eq 'complete') {",
    "  [Console]::Error.WriteLine('fixture diagnostic')",
    "  $result = @{protocol='plugin-hands/1';type='result';launch_id=$env:CHRIPTMAS_PLUGIN_HANDS_LAUNCH_ID;lease_id=$env:CHRIPTMAS_PLUGIN_HANDS_LEASE_ID;invocation_id=$env:CHRIPTMAS_PLUGIN_HANDS_INVOCATION_ID;output=@{value='contained'}}",
    "  [Console]::Out.WriteLine(($result | ConvertTo-Json -Compress))",
    "  exit 0",
    "}",
    "$started=@{pid=$PID;launch_id=$env:CHRIPTMAS_PLUGIN_HANDS_LAUNCH_ID;lease_id=$env:CHRIPTMAS_PLUGIN_HANDS_LEASE_ID;invocation_id=$env:CHRIPTMAS_PLUGIN_HANDS_INVOCATION_ID}|ConvertTo-Json -Compress",
    "[IO.File]::WriteAllText((Join-Path (Get-Location) 'output\\started.json'),$started)",
    "Start-Sleep -Seconds 120",
  ].join("\n");
  fs.writeFileSync(path.join(payload, "main.ps1"), source, "utf8");
  return plugin;
}

async function call(origin, secret, method, route, body) {
  const response = await fetch(`${origin}${route}`, { method, headers: { "Content-Type": "application/json", "X-Chriptmas-Desktop-Session": secret }, body: body === undefined ? undefined : JSON.stringify(body) });
  const payload = await response.json();
  if (!response.ok) throw new Error(`http_${response.status}:${route}:${JSON.stringify(payload)}`);
  return payload;
}

function startedMarker(root) {
  const workspaceRoot = path.join(root, ".rebuild-data", "plugin-hands-workspaces");
  let leases;
  try { leases = fs.readdirSync(workspaceRoot, { withFileTypes: true }); } catch { return null; }
  for (const lease of leases) {
    if (!lease.isDirectory()) continue;
    const candidate = path.join(workspaceRoot, lease.name, "output", "started.json");
    if (fs.statSync(candidate, { throwIfNoEntry: false })?.isFile()) return candidate;
  }
  return null;
}

async function waitForStarted(root, origin, secret, turnId) {
  const deadline = Date.now() + 15000;
  let latest = [];
  while (Date.now() < deadline) {
    const events = await call(origin, secret, "GET", `/api/ai/turns/${turnId}/events`);
    latest = events.events;
    const started = events.events.find((item) => item.type === "tool.started");
    if (started) {
      const response = await fetch(`${origin}/api/ai/governance/plugins/packages/packaged-hand-plugin/hands/long-running/attempts/${started.correlation.tool_call_id}`, { headers: { "X-Chriptmas-Desktop-Session": secret } });
      if (response.ok) {
        const attempt = await response.json();
        if (attempt.attempt.state === "fenced") return { toolCallId: started.correlation.tool_call_id };
      } else if (response.status !== 404) {
        throw new Error(`attempt_projection_unexpected:${response.status}`);
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  const failed = latest.length ? latest[latest.length - 1] : null;
  const developer = await call(origin, secret, "GET", `/api/ai/turns/${turnId}/events?view=developer`);
  const started = latest.find((item) => item.type === "tool.started");
  const markerPath = startedMarker(root);
  const marker = markerPath ? JSON.parse(fs.readFileSync(markerPath, "utf8")) : null;
  const identity = marker ? {
    launch_present: typeof marker.launch_id === "string" && marker.launch_id.length > 0,
    lease_present: typeof marker.lease_id === "string" && marker.lease_id.length > 0,
    invocation_present: typeof marker.invocation_id === "string" && marker.invocation_id.length > 0,
    invocation_matches_event: marker.invocation_id === started?.correlation?.tool_call_id,
  } : null;
  const attempt = started ? await fetch(`${origin}/api/ai/governance/plugins/packages/packaged-hand-plugin/hands/long-running/attempts/${started.correlation.tool_call_id}`, { headers: { "X-Chriptmas-Desktop-Session": secret } }).then(async (response) => ({ status: response.status, payload: await response.json() })) : null;
  throw new Error(`plugin_hand_did_not_start:${JSON.stringify({ projection: developer.projection, type: failed?.type, data: failed?.data, identity, attempt })}`);
}

async function waitForApproval(origin, secret, turnId) {
  const deadline = Date.now() + 15000;
  let latest = [];
  while (Date.now() < deadline) {
    const events = await call(origin, secret, "GET", `/api/ai/turns/${turnId}/events`);
    latest = events.events;
    const approval = events.events.find((item) => item.type === "approval.required");
    if (approval) return { event: approval, sequence: events.events[events.events.length - 1].sequence };
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  const failed = latest.length ? latest[latest.length - 1] : null;
  throw new Error(`plugin_hand_approval_missing:${JSON.stringify({ type: failed?.type, data: failed?.data })}`);
}

async function waitForEvent(origin, secret, turnId, type) {
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    const events = await call(origin, secret, "GET", `/api/ai/turns/${turnId}/events`);
    const event = events.events.find((item) => item.type === type);
    if (event) return event;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`turn_event_missing:${type}`);
}

async function waitForFile(target, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (fs.statSync(target, { throwIfNoEntry: false })?.isFile()) return;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error(`fixture_timeout:${path.basename(target)}`);
}

async function main() {
  requireFreshCandidate();
  const { SidecarSupervisor, SESSION_HEADER } = require(supervisorPath);
  const pythonPath = path.join(sidecarRoot, "runtime", "python.exe");
  const settingsSource = path.join(sidecarRoot, "config", "settings.toml");
  const settingsTarget = path.join(appData, "config", "settings.toml");
  fs.mkdirSync(path.dirname(settingsTarget), { recursive: true });
  fs.copyFileSync(settingsSource, settingsTarget);
  const packagePath = packageSource(appData);
  let supervisor = null;
  let restarted = null;
  try {
    supervisor = new SidecarSupervisor({ rootDir: appData, moduleRoot: sidecarRoot, workingDir: appData, pythonPath, startupTimeoutMs: 45000 });
    const session = await supervisor.start();
    const unauthenticated = await fetch(`${session.origin}/api/health`);
    if (unauthenticated.status !== 403 || SESSION_HEADER !== "X-Chriptmas-Desktop-Session") throw new Error("desktop_session_auth_not_enforced");
    const discovered = await call(session.origin, session.secret, "POST", "/api/ai/governance/plugins/packages/discover", { source_path: packagePath, command_id: "discover-packaged-hand-0001" });
    if (discovered.compatibility_report?.compatible !== true) throw new Error(`package_quarantined:${JSON.stringify(discovered.compatibility_report?.issues || [])}`);
    const installed = await call(session.origin, session.secret, "POST", "/api/ai/governance/plugins/packages/packaged-hand-plugin/install-disabled", { expected_state_revision: discovered.state_revision, command_id: "install-packaged-hand-0001", confirm: true });
    const reviewed = await call(session.origin, session.secret, "POST", "/api/ai/governance/plugins/packages/packaged-hand-plugin/hands/long-running/review", { expected_state_revision: installed.state_revision, command_id: "review-packaged-hand-0001", confirm: true, reason: "real packaged containment gate" });
    const materialized = await call(session.origin, session.secret, "POST", "/api/ai/governance/plugins/packages/packaged-hand-plugin/hands/long-running/materialize", { expected_review_revision: reviewed.review_revision, expected_materialization_revision: 0, command_id: "materialize-packaged-hand-0001", confirm: true });
    await call(session.origin, session.secret, "POST", "/api/ai/governance/plugins/packages/packaged-hand-plugin/hands/long-running/activate", { expected_review_revision: reviewed.review_revision, expected_materialization_revision: materialized.materialization_revision, expected_activation_revision: 0, command_id: "activate-packaged-hand-0001", confirm: true });
    const enabled = await call(session.origin, session.secret, "POST", "/api/ai/governance/plugins/packages/packaged-hand-plugin/hands/long-running/projects/packaged-project/enable", { expected_profile_revision: 0, confirm: true });
    const capabilityId = "plugin.hand.packaged-hand-plugin.long-running";
    if (enabled.tool.tool_id !== capabilityId || enabled.profile.allowed_tool_ids[0] !== capabilityId) throw new Error("project_binding_missing");
    const runtimeProbe = await fetch(`${session.origin}/api/ai/turns/turn-ffffffffffffffffffffffffffffffff/events`, { headers: { "X-Chriptmas-Desktop-Session": session.secret } });
    if (runtimeProbe.status !== 404) throw new Error(`runtime_probe_unexpected:${runtimeProbe.status}`);
    const catalog = await call(session.origin, session.secret, "GET", "/api/ai/projects/packaged-project/capabilities");
    const target = catalog.entries.find((entry) => entry.stable_id === capabilityId && entry.selected && entry.state === "available");
    if (!target) throw new Error(`project_grant_target_missing:${JSON.stringify(catalog.entries)}`);
    const completedTurnId = "turn-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const completedReceipt = await call(session.origin, session.secret, "POST", "/api/ai/turns", { schema_version: "1.0.0", turn_id: completedTurnId, session_id: "packaged-session", operation_id: "op-packaged-hand-complete-0001", idempotency_key: "packaged-hand-complete-0001", scope: { kind: "project", project_id: "packaged-project", series_id: null }, input: { kind: "text", text: "complete reviewed local hand", refs: [] }, desired_outcome: capabilityId, privacy: { mode: "local_only", allow_remote: false, pii: "none", consent_refs: [], retention: "local_durable" }, capability_policy: { allowed: [capabilityId], denied: [], require_approval: [] }, context_policy: { include_project_skill: false, include_memory: false, include_session_history: false, max_context_bytes: 1024 }, approval_policy: { mode: "risk_based", auto_approve_read_only: true }, capability_request: { mode: "execute_exact_v1", capability_id: capabilityId, arguments: { value: "complete" } }, created_at: "2026-08-26T00:00:00Z" });
    if (completedReceipt.status !== "accepted") throw new Error("completed_turn_not_accepted");
    const completedApproval = await waitForApproval(session.origin, session.secret, completedTurnId);
    await call(session.origin, session.secret, "POST", `/api/ai/turns/${completedTurnId}/actions`, { schema_version: "1.0.0", action_id: "action-packaged-complete-000000001", turn_id: completedTurnId, type: "approve", target_event_id: completedApproval.event.event_id, reason: "prove complete packaged execution", actor: "user", expected_sequence: completedApproval.sequence, idempotency_key: "approve-packaged-complete-0001", created_at: "2026-08-26T00:00:00Z" });
    await waitForEvent(session.origin, session.secret, completedTurnId, "tool.completed");
    const turnId = "turn-0123456789abcdef0123456789abcdef";
    const receipt = await call(session.origin, session.secret, "POST", "/api/ai/turns", { schema_version: "1.0.0", turn_id: turnId, session_id: "packaged-session", operation_id: "op-packaged-hand-vertical-0001", idempotency_key: "packaged-hand-vertical-0001", scope: { kind: "project", project_id: "packaged-project", series_id: null }, input: { kind: "text", text: "execute reviewed local hand", refs: [] }, desired_outcome: capabilityId, privacy: { mode: "local_only", allow_remote: false, pii: "none", consent_refs: [], retention: "local_durable" }, capability_policy: { allowed: [capabilityId], denied: [], require_approval: [] }, context_policy: { include_project_skill: false, include_memory: false, include_session_history: false, max_context_bytes: 1024 }, approval_policy: { mode: "risk_based", auto_approve_read_only: true }, capability_request: { mode: "execute_exact_v1", capability_id: capabilityId, arguments: { value: "gate" } }, created_at: "2026-08-26T00:00:00Z" });
    if (receipt.status !== "accepted") throw new Error("turn_not_accepted");
    const approval = await waitForApproval(session.origin, session.secret, turnId);
    const approvalRequest = call(session.origin, session.secret, "POST", `/api/ai/turns/${turnId}/actions`, { schema_version: "1.0.0", action_id: "action-packaged-hand-000000000001", turn_id: turnId, type: "approve", target_event_id: approval.event.event_id, reason: "approved packaged containment gate", actor: "user", expected_sequence: approval.sequence, idempotency_key: "approve-packaged-hand-0001", created_at: "2026-08-26T00:00:00Z" }).catch(() => null);
    const started = await waitForStarted(appData, session.origin, session.secret, turnId);
    const beforeStop = await call(session.origin, session.secret, "GET", `/api/ai/governance/plugins/packages/packaged-hand-plugin/hands/long-running/attempts/${started.toolCallId}`);
    if (beforeStop.attempt.state !== "fenced" || beforeStop.attempt.workspace_retention !== "not_yet_terminal") throw new Error("durable_fence_missing");
    const sidecar = { pid: session.child_pid };
    writeJson(readyPath, { sidecar, attempt_id: started.toolCallId });
    await waitForFile(stopPath);
    await supervisor.stop(); supervisor = null;
    await approvalRequest;
    restarted = new SidecarSupervisor({ rootDir: appData, moduleRoot: sidecarRoot, workingDir: appData, pythonPath, startupTimeoutMs: 45000 });
    const recovery = await restarted.start();
    const afterRestart = await call(recovery.origin, recovery.secret, "GET", `/api/ai/governance/plugins/packages/packaged-hand-plugin/hands/long-running/attempts/${started.toolCallId}`);
    const recoveredEvents = await call(recovery.origin, recovery.secret, "GET", `/api/ai/turns/${turnId}/events`);
    const startedCount = recoveredEvents.events.filter((item) => item.type === "tool.started").length;
    if (startedCount !== 1) throw new Error(`durable_replay_detected:${startedCount}`);
    await restarted.stop(); restarted = null;
    writeJson(resultPath, { status: "ok", sidecar, attempt_id: started.toolCallId, before_state: beforeStop.attempt.state, after_state: afterRestart.attempt.state, workspace_retention: afterRestart.attempt.workspace_retention, started_count: startedCount });
  } finally {
    if (restarted) await restarted.stop().catch(() => {});
    if (supervisor) await supervisor.stop().catch(() => {});
  }
}

main().catch((error) => { writeJson(resultPath, { status: "failed", error: String(error.stack || error) }); process.exitCode = 1; });
