const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

function packageSource(vaultRoot) {
  const plugin = path.join(vaultRoot, ".rebuild-data", "plugin-package-inbox", "electron-hook-plugin");
  const hand = path.join(plugin, "hands", "policy-hand");
  const payload = path.join(hand, "payload");
  const hook = path.join(plugin, "hooks", "pre-tool-policy");
  fs.mkdirSync(path.join(plugin, ".codex-plugin"), { recursive: true });
  fs.mkdirSync(payload, { recursive: true });
  fs.mkdirSync(hook, { recursive: true });
  fs.writeFileSync(path.join(plugin, ".codex-plugin", "plugin.json"), JSON.stringify({ name: "electron-hook-plugin", version: "1.0.0", description: "Electron Hook fault Gate" }));
  const inputSchema = { type: "object", properties: { hook_event: { type: "string" }, payload: { type: "object" } }, required: ["hook_event", "payload"], additionalProperties: false };
  const outputSchema = { type: "object", properties: { exit_code: { type: "integer" }, stdout: { type: "string" }, stderr: { type: "string" } }, required: ["exit_code", "stdout", "stderr"], additionalProperties: false };
  fs.writeFileSync(path.join(hand, "hand.json"), JSON.stringify({ schema_version: "1.0.0", id: "policy-hand", runtime: "powershell-stdio-v1", entrypoint: "payload/main.ps1", input_schema: inputSchema, output_schema: outputSchema, effect: "read", operation_semantics: "read_only", requested_resources: [] }));
  fs.writeFileSync(path.join(hook, "hook.json"), JSON.stringify({ schema_version: "1.0.0", id: "pre-tool-policy", hand_id: "policy-hand", event: "PreToolUse", order: 10, sync: true, timeout_ms: 10000, metadata_projection: "codex-hook-v1", recursion: "deny" }));
  const denied = JSON.stringify({ hookSpecificOutput: { hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: "electron contained policy" } }).replaceAll("'", "''");
  const source = [
    "$hello=@{protocol='plugin-hands/1';type='hello';launch_id=$env:CHRIPTMAS_PLUGIN_HANDS_LAUNCH_ID;lease_id=$env:CHRIPTMAS_PLUGIN_HANDS_LEASE_ID;invocation_id=$env:CHRIPTMAS_PLUGIN_HANDS_INVOCATION_ID}",
    "[Console]::Out.WriteLine(($hello|ConvertTo-Json -Compress))",
    "$request=[Console]::In.ReadLine()|ConvertFrom-Json",
    "if($request.input.payload.turn_id -eq 'turn-55555555555555555555555555555555'){$stdout='" + denied + "'}else{$stdout=''}",
    "$result=@{protocol='plugin-hands/1';type='result';launch_id=$env:CHRIPTMAS_PLUGIN_HANDS_LAUNCH_ID;lease_id=$env:CHRIPTMAS_PLUGIN_HANDS_LEASE_ID;invocation_id=$env:CHRIPTMAS_PLUGIN_HANDS_INVOCATION_ID;output=@{exit_code=0;stdout=$stdout;stderr=''}}",
    "[Console]::Out.WriteLine(($result|ConvertTo-Json -Depth 8 -Compress))",
  ].join("\n");
  fs.writeFileSync(path.join(payload, "main.ps1"), source, "utf8");
  return plugin;
}

async function call(page, method, route, body) {
  return page.evaluate(`(async()=>{const response=await fetch(window.electronAPI.backendBaseUrl+${JSON.stringify(route)},{method:${JSON.stringify(method)},headers:{'Content-Type':'application/json'},body:${body === undefined ? "undefined" : `JSON.stringify(${JSON.stringify(body)})`}});return {status:response.status,payload:await response.json()}})()`);
}

function requireOk(result, label) {
  if (result.status < 200 || result.status >= 300) throw new Error(`${label}:${JSON.stringify(result)}`);
  return result.payload;
}

async function waitForEvent(page, turnId, type, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  let observed = [];
  while (Date.now() < deadline) {
    const result = await call(page, "GET", `/api/ai/turns/${turnId}/events`);
    if (result.status === 200) {
      observed = result.payload.events;
      const event = result.payload.events.find((item) => item.type === type);
      if (event) return { event, events: result.payload.events };
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`electron_hook_event_timeout:${turnId}:${type}:${JSON.stringify(observed)}`);
}

function sqliteJsonRows(pythonPath, database, sql, parameters) {
  const source = "import json,pathlib,sqlite3,sys; u=pathlib.Path(sys.argv[1]).resolve().as_uri()+'?mode=ro'; c=sqlite3.connect(u,uri=True); rows=c.execute(sys.argv[2],json.loads(sys.argv[3])).fetchall(); print(json.dumps([json.loads(r[0]) for r in rows],separators=(',',':')))";
  const result = spawnSync(pythonPath, ["-I", "-c", source, database, sql, JSON.stringify(parameters)], { encoding: "utf8", windowsHide: true });
  if (result.status !== 0) throw new Error(`electron_hook_sqlite_read_failed:${String(result.stderr).slice(-1000)}`);
  return JSON.parse(result.stdout);
}

function turnRequest(turnId, capabilityId) {
  return { schema_version: "1.0.0", turn_id: turnId, session_id: "electron-hook-session", operation_id: `op-${turnId}`, idempotency_key: `idem-${turnId}`, scope: { kind: "project", project_id: "electron-hook-project", series_id: null }, input: { kind: "text", text: "execute frozen Hook fault Gate", refs: [] }, desired_outcome: capabilityId, privacy: { mode: "local_only", allow_remote: false, pii: "none", consent_refs: [], retention: "local_durable" }, capability_policy: { allowed: [capabilityId], denied: [], require_approval: [] }, context_policy: { include_project_skill: false, include_memory: false, include_session_history: false, max_context_bytes: 1024 }, approval_policy: { mode: "risk_based", auto_approve_read_only: true }, capability_request: { mode: "execute_exact_v1", capability_id: capabilityId, arguments: { hook_event: "fixture", payload: { mode: "tool" } } }, created_at: "2026-08-27T00:00:00Z" };
}

async function runPluginHookElectronFaultGate(session, temporaryRoot, pythonPath) {
  const vault = path.join(temporaryRoot, "vault");
  const plugin = packageSource(vault);
  const discovered = requireOk(await call(session.page, "POST", "/api/ai/governance/plugins/packages/discover", { source_path: plugin, command_id: "electron-hook-discover-0001" }), "discover");
  const installed = requireOk(await call(session.page, "POST", "/api/ai/governance/plugins/packages/electron-hook-plugin/install-disabled", { expected_state_revision: discovered.state_revision, command_id: "electron-hook-install-0001", confirm: true }), "install");
  const handReview = requireOk(await call(session.page, "POST", "/api/ai/governance/plugins/packages/electron-hook-plugin/hands/policy-hand/review", { expected_state_revision: installed.state_revision, command_id: "electron-hook-hand-review-0001", confirm: true, reason: "Electron fault Gate" }), "hand-review");
  const materialized = requireOk(await call(session.page, "POST", "/api/ai/governance/plugins/packages/electron-hook-plugin/hands/policy-hand/materialize", { expected_review_revision: handReview.review_revision, expected_materialization_revision: 0, command_id: "electron-hook-materialize-0001", confirm: true }), "materialize");
  const handActive = requireOk(await call(session.page, "POST", "/api/ai/governance/plugins/packages/electron-hook-plugin/hands/policy-hand/activate", { expected_review_revision: handReview.review_revision, expected_materialization_revision: materialized.materialization_revision, expected_activation_revision: 0, command_id: "electron-hook-hand-activate-0001", confirm: true }), "hand-activate");
  requireOk(await call(session.page, "POST", "/api/ai/governance/plugins/packages/electron-hook-plugin/hands/policy-hand/projects/electron-hook-project/enable", { expected_profile_revision: 0, confirm: true }), "project-enable");
  const hookReview = requireOk(await call(session.page, "POST", "/api/ai/governance/plugins/packages/electron-hook-plugin/hooks/pre-tool-policy/review", { expected_state_revision: installed.state_revision, expected_hand_activation_revision: handActive.activation_revision, command_id: "electron-hook-review-0001", confirm: true, reason: "Electron direct deny" }), "hook-review");
  const hookActive = requireOk(await call(session.page, "POST", "/api/ai/governance/plugins/packages/electron-hook-plugin/hooks/pre-tool-policy/activate", { expected_review_revision: hookReview.review_revision, expected_activation_revision: 0, command_id: "electron-hook-activate-0001", confirm: true }), "hook-activate");
  const capabilityId = "plugin.hand.electron-hook-plugin.policy-hand";
  const frozenTurn = "turn-44444444444444444444444444444444";
  const marker = path.join(vault, ".rebuild-data", "e2e-plugin-hook-projection-fault");
  fs.writeFileSync(marker, "fail", "ascii");
  const failedDisable = await call(session.page, "POST", "/api/ai/governance/plugins/packages/electron-hook-plugin/hooks/pre-tool-policy/disable", { expected_activation_revision: hookActive.activation_revision, command_id: "electron-hook-disable-0001", reason: "inject projection failure" });
  if (failedDisable.status !== 503 || failedDisable.payload.status !== "plugin_hook_runtime_unavailable") throw new Error(`projection_failure_not_observed:${JSON.stringify(failedDisable)}`);
  requireOk(await call(session.page, "POST", "/api/ai/turns", turnRequest(frozenTurn, capabilityId)), "turn-submit");
  let completed;
  try {
    completed = await waitForEvent(session.page, frozenTurn, "tool.completed");
  } catch (error) {
    const diagnostics = sqliteJsonRows(pythonPath, path.join(vault, ".rebuild-data", "ai-turns.sqlite3"), "SELECT json_object('kind',kind,'payload',json(payload_json)) FROM ai_turn_payloads WHERE turn_id=?", [frozenTurn]);
    throw new Error(`${error.message}:payloads=${JSON.stringify(diagnostics)}`);
  }
  const snapshots = sqliteJsonRows(pythonPath, path.join(vault, ".rebuild-data", "ai-turns.sqlite3"), "SELECT payload_json FROM ai_turn_immutable_payloads WHERE turn_id=? AND kind='codex-hook-policy-snapshot'", [frozenTurn]);
  if (snapshots.length !== 1 || snapshots[0].handlers.length !== 0) throw new Error(`failed_projection_not_fail_closed:${JSON.stringify(snapshots)}`);
  const receipts = sqliteJsonRows(pythonPath, path.join(vault, ".rebuild-data", "ai-turns.sqlite3"), "SELECT payload_json FROM ai_turn_payloads WHERE turn_id=? AND kind='codex-hook-invocation-receipt'", [frozenTurn]);
  if (receipts.length !== 1 || receipts[0].handler_runs.length !== 0 || receipts[0].normalized_outcome.dispatch_blocked !== false) throw new Error(`frozen_hook_not_fenced:${JSON.stringify(receipts)}`);
  fs.unlinkSync(marker);
  const reactivated = requireOk(await call(session.page, "POST", "/api/ai/governance/plugins/packages/electron-hook-plugin/hooks/pre-tool-policy/activate", { expected_review_revision: hookReview.review_revision, expected_activation_revision: hookActive.activation_revision + 1, command_id: "electron-hook-reactivate-0001", confirm: true }), "hook-reactivate");
  const deniedTurn = "turn-55555555555555555555555555555555";
  requireOk(await call(session.page, "POST", "/api/ai/turns", turnRequest(deniedTurn, capabilityId)), "denied-submit");
  const denied = await waitForEvent(session.page, deniedTurn, "turn.failed");
  if (denied.events.some((item) => item.type === "tool.started")) throw new Error("reactivated_hook_did_not_deny_before_tool");
  const deniedReceipts = sqliteJsonRows(pythonPath, path.join(vault, ".rebuild-data", "ai-turns.sqlite3"), "SELECT payload_json FROM ai_turn_payloads WHERE turn_id=? AND kind='codex-hook-invocation-receipt'", [deniedTurn]);
  const deniedReceipt = deniedReceipts[0];
  if (deniedReceipts.length !== 1 || deniedReceipt.normalized_outcome?.dispatch_blocked !== true || deniedReceipt.normalized_outcome?.reason !== "electron contained policy" || deniedReceipt.handler_runs?.length !== 1 || deniedReceipt.handler_runs[0].handler_id !== "plugin.electron-hook-plugin.pre-tool-policy" || deniedReceipt.handler_runs[0].status !== "blocked") throw new Error(`reactivated_hook_receipt_invalid:${JSON.stringify(deniedReceipts)}`);
  return { electron_pid: session.child.pid, sidecar_pid: session.sidecarPid, projection_fail_closed_snapshot: true, projection_failure_status: failedDisable.status, failed_projection_handler_runs: receipts[0].handler_runs.length, bound_hand_tool_completed: completed.event.type === "tool.completed", reactivated_revision: reactivated.activation_revision, reactivated_handler: deniedReceipt.handler_runs[0].handler_id, reactivated_denied: true };
}

module.exports = { runPluginHookElectronFaultGate };
