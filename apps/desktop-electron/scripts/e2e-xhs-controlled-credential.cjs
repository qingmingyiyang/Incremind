const fs = require("node:fs");
const path = require("node:path");
const { randomBytes, randomUUID } = require("node:crypto");
const { spawnSync } = require("node:child_process");

const FIXTURE_URL = "https://www.xiaohongshu.com/explore/e2e000000000000000000002";
const FIXTURE_COOKIE_CANARY = "xhs-e2e-controlled-cookie-canary-r1";
const PROJECT_ID = "default";
const SUBJECT = "xhs-e2e-fixture-account";
const BINARY_ARRIVED_RELATIVE_PATH = path.join(".rebuild-data", "xhs-controlled-credential-e2e", "binary-arrived.json");
const BINARY_RELEASE_RELATIVE_PATH = path.join(".rebuild-data", "xhs-controlled-credential-e2e", "binary-release");
const BINARY_TRACE_RELATIVE_PATH = path.join(".rebuild-data", "xhs-controlled-credential-e2e", "binary-trace.json");

async function rendererApi(page, route, { method = "GET", body } = {}) {
  return page.evaluate(`(async () => {
    const response = await fetch(window.electronAPI.backendBaseUrl + ${JSON.stringify(route)}, {
      method: ${JSON.stringify(method)},
      headers: ${body === undefined ? "{}" : "{'Content-Type':'application/json'}"},
      body: ${body === undefined ? "undefined" : JSON.stringify(JSON.stringify(body))},
    });
    const text = await response.text();
    let payload = null; try { payload = JSON.parse(text); } catch {}
    return { status: response.status, payload, text: text.slice(0, 1000) };
  })()`);
}

function requireStatus(result, status, label) {
  if (result.status !== status) {
    throw new Error(`${label}: unexpected HTTP ${result.status}: ${JSON.stringify(result.payload ?? result.text)}`);
  }
  return result.payload;
}

async function waitFor(action, label, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  let last = null;
  while (Date.now() < deadline) {
    try { return await action(); } catch (error) {
      if (error?.fatal === true) throw error;
      last = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 80));
  }
  throw new Error(`${label}: ${last?.message || "timed out"}`);
}

async function operateVisiblePanel(page, operation) {
  const result = await page.evaluate(`(async () => {
    const projectId = ${JSON.stringify(PROJECT_ID)};
    const subject = ${JSON.stringify(SUBJECT)};
    const cookie = ${JSON.stringify(FIXTURE_COOKIE_CANARY)};
    window.localStorage.setItem('chriptmas-os-onboarding-v2-complete', 'true');
    document.querySelector('.first-run-onboarding-close')?.click();
    window.confirm = () => true;
    window.location.hash = '#view=rebuild-settings&project_id=' + encodeURIComponent(projectId);
    const until = Date.now() + 20000;
    let panel = null;
    while (Date.now() < until) {
      panel = document.querySelector('[aria-label="小红书受控凭据"]');
      if (panel) break;
      await new Promise((resolve) => setTimeout(resolve, 80));
    }
    if (!panel) throw new Error('controlled credential Panel was not visibly rendered');
    if (!panel.textContent.includes(projectId)) throw new Error('controlled credential Panel did not bind explicit project_id');
    const setValue = (node, value) => {
      const proto = node instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, 'value').set.call(node, value);
      node.dispatchEvent(new Event('input', { bubbles: true }));
      node.dispatchEvent(new Event('change', { bubbles: true }));
    };
    const inputs = panel.querySelectorAll('input');
    const textarea = panel.querySelector('textarea');
    if (inputs.length < 2 || !textarea) throw new Error('controlled credential Panel controls missing');
    setValue(inputs[0], subject);
    setValue(inputs[1], '2030-01-02T03:04');
    if (${JSON.stringify(operation)} !== 'revoke') setValue(textarea, cookie);
    const buttonText = ${JSON.stringify(operation)} === 'revoke' ? '撤销授权' : ${JSON.stringify(operation)} === 'rotate' ? '轮换 Cookie' : '授权 Cookie';
    while (Date.now() < until) {
      const button = [...panel.querySelectorAll('button')].find((node) => node.textContent.trim() === buttonText);
      if (button && !button.disabled) { button.click(); break; }
      await new Promise((resolve) => setTimeout(resolve, 80));
    }
    while (Date.now() < until) {
      const text = panel.innerText;
      const settled = ${JSON.stringify(operation)} === 'revoke'
        ? text.includes('撤销操作已完成')
        : ${JSON.stringify(operation)} === 'rotate'
          ? text.includes('轮换操作已完成')
          : text.includes('授权操作已完成');
      if (settled) {
        return {
          textarea_cleared: textarea.value === '',
          public_ui_has_canary: document.body.innerText.includes(cookie),
          state: panel.querySelector('header strong')?.textContent?.trim() || null,
          authorization_revision: [...panel.querySelectorAll('.xhs-controlled-credential-metadata dd')][0]?.textContent?.trim() || null,
        };
      }
      await new Promise((resolve) => setTimeout(resolve, 80));
    }
    throw new Error('controlled credential Panel operation did not settle');
  })()`);
  if (!result.textarea_cleared || result.public_ui_has_canary) {
    throw new Error('controlled credential Panel exposed or retained the Cookie');
  }
  return result;
}

function controlledTurn(turnId, input) {
  const suffix = randomBytes(12).toString("hex");
  return {
    schema_version: "1.0.0", turn_id: turnId, session_id: `xhs-e2e-session-${suffix}`,
    operation_id: `xhs-e2e-operation-${suffix}`, idempotency_key: `xhs-e2e-idempotency-${suffix}`,
    scope: { kind: "project", project_id: PROJECT_ID, series_id: null },
    input: { kind: "text", text: "Resolve governed controlled Xiaohongshu metadata.", refs: [] },
    desired_outcome: "analyze_source",
    privacy: { mode: "remote_allowed", allow_remote: true, pii: "none", consent_refs: [], retention: "local_durable" },
    capability_policy: { allowed: ["analyze_source"], denied: [], require_approval: ["analyze_source"] },
    context_policy: { include_project_skill: false, include_memory: false, include_session_history: false, max_context_bytes: 4096 },
    approval_policy: { mode: "explicit", auto_approve_read_only: true },
    capability_request: {
      mode: "execute_exact_v1", capability_id: "analyze_source",
      arguments: {
        input,
        intent: "organize",
        output_profile: { profile_id: "default", revision: "1" },
        resource_budget: { max_assets: 8, max_bytes: 1048576, max_seconds: 30 },
        access: { mode: "controlled_credential", credential_subject_id: SUBJECT },
      },
    },
    created_at: "2026-08-27T00:00:00+00:00",
  };
}

async function submitAndApproveControlledResolve(page, input, { pythonPath, temporaryRoot } = {}) {
  const turnId = `turn-${randomUUID().replaceAll("-", "")}`;
  requireStatus(await rendererApi(page, "/api/ai/turns", { method: "POST", body: controlledTurn(turnId, input) }), 202, "controlled resolve Turn submit");
  const approval = await waitFor(async () => {
    const events = requireStatus(await rendererApi(page, `/api/ai/turns/${turnId}/events`), 200, "controlled Turn events").events;
    const event = Array.isArray(events) ? events.find((item) => item.type === "approval.required") : null;
    const terminal = Array.isArray(events) ? events.find((item) => /^turn\.(completed|failed|cancelled)$/.test(String(item.type))) : null;
    if (terminal) {
      let payloads = [];
      if (pythonPath && temporaryRoot) {
        payloads = sqlitePayloadRows(
          pythonPath,
          path.join(temporaryRoot, "vault", ".rebuild-data", "ai-turns.sqlite3"),
          "SELECT json_object('kind',kind,'payload',json(payload_json)) FROM ai_turn_payloads WHERE turn_id=? UNION ALL SELECT json_object('kind',kind,'payload',json(payload_json)) FROM ai_turn_immutable_payloads WHERE turn_id=?",
          [turnId, turnId],
        ).map(({ kind, payload }) => ({
          kind,
          capability_ids: payload?.capability_ids,
          excluded_reason_counts: payload?.excluded_reason_counts,
          manifest_id: payload?.manifest_id,
          capability_request: payload?.capability_request,
          error: payload?.error,
        }));
      }
      throw new Error(`controlled Turn terminated before approval: ${JSON.stringify({ events, payloads })}`);
    }
    if (!event || !Number.isInteger(event.sequence) || typeof event.event_id !== "string") throw new Error("approval is pending");
    return event;
  }, "controlled analyze_source approval");
  const action = {
    schema_version: "1.0.0", action_id: `action-${randomUUID().replaceAll("-", "")}`,
    turn_id: turnId, type: "approve", target_event_id: approval.event_id,
    reason: "Packaged controlled credential Gate approval", actor: "user",
    expected_sequence: approval.sequence, idempotency_key: `approve-xhs-e2e-${randomBytes(12).toString("hex")}`,
    created_at: "2026-08-27T00:00:01+00:00",
  };
  await waitFor(async () => {
    const result = await rendererApi(page, `/api/ai/turns/${turnId}/actions`, { method: "POST", body: action });
    if (result.status === 400 && result.payload?.reason === "AI Turn action is already running") {
      throw new Error("controlled Turn runner lease is still converging");
    }
    return requireStatus(result, 202, "controlled resolve Turn approve");
  }, "controlled resolve Turn approve");
  const terminal = await waitFor(async () => {
    const body = requireStatus(await rendererApi(page, `/api/ai/turns/${turnId}/events`), 200, "controlled Turn terminal events");
    const event = body.events?.find((item) => /^turn\.(completed|failed|cancelled)$/.test(String(item.type)));
    if (!event) throw new Error("controlled Turn terminal event pending");
    return { event, events: body.events };
  }, "controlled analyze_source terminal");
  return { turnId, terminal };
}

function sqlitePayloadRows(pythonPath, database, sql, parameters) {
  const source = "import json,pathlib,sqlite3,sys;u=pathlib.Path(sys.argv[1]).resolve().as_uri()+'?mode=ro';c=sqlite3.connect(u,uri=True);rows=c.execute(sys.argv[2],json.loads(sys.argv[3])).fetchall();print(json.dumps([json.loads(r[0]) for r in rows],separators=(',',':')))";
  const result = spawnSync(pythonPath, ["-I", "-c", source, database, sql, JSON.stringify(parameters)], { encoding: "utf8", windowsHide: true });
  if (result.status !== 0) throw new Error(`controlled credential SQLite mode=ro read failed: ${String(result.stderr).slice(-500)}`);
  return JSON.parse(result.stdout);
}

function findManifestArtifact(temporaryRoot, manifestRef) {
  const root = path.join(temporaryRoot, "vault", ".rebuild-data", "objects", "default", "source_manifests");
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    if (!entry.isFile() || !entry.name.endsWith(".json") || entry.name.endsWith(".meta.json")) continue;
    const value = JSON.parse(fs.readFileSync(path.join(root, entry.name), "utf8"));
    if (value?.public_ref === manifestRef) return value;
  }
  throw new Error("controlled credential Manifest artifact was not found");
}

function controlledResolveEvidence(temporaryRoot, pythonPath, turnId) {
  const database = path.join(temporaryRoot, "vault", ".rebuild-data", "ai-turns.sqlite3");
  const receipts = sqlitePayloadRows(pythonPath, database,
    "SELECT payload_json FROM ai_turn_immutable_payloads WHERE turn_id=? AND kind LIKE 'analyze-source-receipt:%'", [turnId]);
  if (receipts.length !== 1 || receipts[0]?.outcome?.reason !== "source_permission_unresolved") {
    throw new Error("controlled resolve did not produce the expected immutable terminal receipt");
  }
  const manifestRef = receipts[0].outcome.manifest_ref;
  if (typeof manifestRef !== "string" || !receipts[0].outcome?.credential_use) throw new Error("controlled receipt lacks non-secret credential binding evidence");
  const artifact = findManifestArtifact(temporaryRoot, manifestRef);
  if (JSON.stringify(receipts[0]).includes(FIXTURE_COOKIE_CANARY) || JSON.stringify(artifact).includes(FIXTURE_COOKIE_CANARY)) {
    throw new Error("controlled credential leaked into receipt or Manifest artifact");
  }
  return { receipt: receipts[0], artifact, manifestRef };
}

function assertGovernedTurnEvidence(temporaryRoot, pythonPath, turnId) {
  const database = path.join(temporaryRoot, "vault", ".rebuild-data", "ai-turns.sqlite3");
  const rows = sqlitePayloadRows(
    pythonPath,
    database,
    "SELECT json_object('kind',kind,'payload',json(payload_json)) FROM ai_turn_payloads WHERE turn_id=? UNION ALL SELECT json_object('kind',kind,'payload',json(payload_json)) FROM ai_turn_immutable_payloads WHERE turn_id=?",
    [turnId, turnId],
  );
  const kinds = new Set(rows.map((row) => row.kind));
  if (!kinds.has("frozen-tool-authorization-facts-v1")) {
    throw new Error("controlled Turn lacks frozen authorization facts");
  }
  const hookReceipts = rows.filter((row) => String(row.kind).startsWith("codex-hook-invocation-receipt"));
  if (!hookReceipts.some((row) => row.payload?.event === "PreToolUse")) {
    throw new Error("controlled Turn lacks authoritative PreToolUse Hook receipt");
  }
  if (JSON.stringify(rows).includes(FIXTURE_COOKIE_CANARY)) {
    throw new Error("controlled credential leaked into governed Turn evidence");
  }
  return { frozen_authorization: true, pre_tool_hook_receipt: true };
}

async function assertRevokedControlledResolve(page, temporaryRoot, pythonPath) {
  const stopped = await submitAndApproveControlledResolve(page, {
    kind: "text", text: FIXTURE_URL, source_ref: null,
  }, { pythonPath, temporaryRoot });
  const database = path.join(temporaryRoot, "vault", ".rebuild-data", "ai-turns.sqlite3");
  const receipts = sqlitePayloadRows(pythonPath, database,
    "SELECT payload_json FROM ai_turn_immutable_payloads WHERE turn_id=? AND kind LIKE 'analyze-source-receipt:%'", [stopped.turnId]);
  if (receipts.length !== 1 || receipts[0]?.outcome?.reason !== "controlled_credential_pre_wire_drift") {
    throw new Error(`revoked controlled credential did not fail closed on a new resolve: ${JSON.stringify(receipts)}`);
  }
  return true;
}

function assertNoOutputCanary(output, label) {
  if (String(output || "").includes(FIXTURE_COOKIE_CANARY)) {
    throw new Error(`controlled credential leaked into ${label}`);
  }
  return true;
}

function assertSecretIsolation(temporaryRoot) {
  const allowedSecretRoot = path.join(temporaryRoot, "windows-appdata", "Local", "Chriptmas_Replay", "secrets");
  const hits = [];
  const visit = (directory) => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const file = path.join(directory, entry.name);
      if (entry.isDirectory()) visit(file);
      else if (entry.isFile() && fs.readFileSync(file).includes(Buffer.from(FIXTURE_COOKIE_CANARY))) hits.push(file);
    }
  };
  visit(temporaryRoot);
  if (hits.length) throw new Error("controlled credential canary appeared as plaintext outside DPAPI ciphertext storage");
  if (!fs.existsSync(allowedSecretRoot) || !fs.readdirSync(allowedSecretRoot).some((entry) => entry.endsWith(".json"))) {
    throw new Error("DPAPI SecretStore ciphertext file was not created in isolated app data");
  }
  return { plaintext_canary_hits: 0, dpapi_secret_store_present: true };
}

async function runXhsControlledCredentialElectronGate(session, temporaryRoot, pythonPath) {
  requireStatus(await rendererApi(session.page, "/api/ai/media-ingress-selection/revisions", {
    method: "POST",
    body: { command_id: "select-xhs-controlled-hands-0001", expected_revision: 0, confirm: true, mode: "hands" },
  }), 200, "controlled credential Hands selection");
  const catalog = requireStatus(
    await rendererApi(session.page, `/api/ai/projects/${encodeURIComponent(PROJECT_ID)}/capabilities`),
    200,
    "controlled credential capability catalog",
  );
  const analyzeSource = catalog.entries?.find((item) => item.stable_id === "analyze_source" && item.state === "available");
  if (!analyzeSource) throw new Error(`analyze_source is absent from the project catalog: ${JSON.stringify(catalog)}`);
  const granted = await operateVisiblePanel(session.page, "grant");
  if (granted.state !== "已授权") throw new Error("visible controlled credential grant did not become active");
  const diagnostic = { pythonPath, temporaryRoot };
  const resolved = await submitAndApproveControlledResolve(session.page, {
    kind: "text", text: FIXTURE_URL, source_ref: null,
  }, diagnostic);
  const database = path.join(temporaryRoot, "vault", ".rebuild-data", "ai-turns.sqlite3");
  const first = controlledResolveEvidence(temporaryRoot, pythonPath, resolved.turnId);
  const governedTurn = assertGovernedTurnEvidence(temporaryRoot, pythonPath, resolved.turnId);
  const artifact = first.artifact;
  const binding = artifact?.manifest?.credential_binding;
  if (binding?.mode !== "controlled_credential" || binding.provider !== "xiaohongshu" || binding.credential_subject_id !== SUBJECT || binding.secret_generation !== 1) {
    throw new Error("production Manifest did not freeze controlled credential binding");
  }
  const rotated = await operateVisiblePanel(session.page, "rotate");
  if (rotated.state !== "已授权") throw new Error("visible controlled credential rotation did not remain active");
  const resolvedAfterRotation = await submitAndApproveControlledResolve(session.page, {
    kind: "text", text: FIXTURE_URL, source_ref: null,
  }, diagnostic);
  const second = controlledResolveEvidence(temporaryRoot, pythonPath, resolvedAfterRotation.turnId);
  const secondBinding = second.artifact?.manifest?.credential_binding;
  if (second.manifestRef === first.manifestRef || secondBinding?.secret_generation !== 2 || secondBinding?.authorization_revision !== 2) {
    throw new Error("controlled credential rotation did not freeze a new immutable Manifest identity");
  }
  const revoked = await operateVisiblePanel(session.page, "revoke");
  if (revoked.state !== "已撤销") throw new Error("visible controlled credential revoke did not become inactive");
  // This Gate proves the immediately available resolve fence.  A frozen
  // source_ref only becomes an execution attempt after a separate source
  // permission grant and Media Hands admission; that execution-specific
  // revocation drill remains intentionally outside this text-only fixture.
  await assertRevokedControlledResolve(session.page, temporaryRoot, pythonPath);
  assertNoOutputCanary(session.childOutput, "packaged Electron or sidecar output");
  return {
    electron_pid: session.child.pid, sidecar_pid: session.sidecarPid,
    visible_grant: true, textarea_cleared: true, controlled_turn_approved: true,
    immutable_manifest_binding: true, immutable_receipt_redacted: true,
    rotation_new_immutable_manifest: true,
    revoked_new_resolve_stopped: true,
    governed_turn_evidence: governedTurn,
    frozen_source_ref_execution_covered: false,
    frozen_source_ref_execution_gap: "requires a separately granted Media Hands execution drill",
  };
}

function admittedResolveEvidence(temporaryRoot, pythonPath, turnId) {
  const database = path.join(temporaryRoot, "vault", ".rebuild-data", "ai-turns.sqlite3");
  const receipts = sqlitePayloadRows(pythonPath, database,
    "SELECT payload_json FROM ai_turn_immutable_payloads WHERE turn_id=? AND kind LIKE 'analyze-source-receipt:%'", [turnId]);
  const outcome = receipts[0]?.outcome;
  if (receipts.length !== 1 || outcome?.status !== "admitted" || typeof outcome.job_id !== "string" || outcome?.credential_use?.result !== "admitted") {
    throw new Error(`frozen source_ref was not admitted by the production Tool: ${JSON.stringify(receipts)}`);
  }
  if (JSON.stringify(receipts[0]).includes(FIXTURE_COOKIE_CANARY)) {
    throw new Error("controlled credential leaked into admitted analyze_source Receipt");
  }
  return { receipt: receipts[0], jobId: outcome.job_id };
}

function inspectBinaryRevocationAuthority(temporaryRoot, pythonPath, jobId) {
  const database = path.join(temporaryRoot, "vault", ".rebuild-data", "jobs.sqlite3");
  const rows = sqlitePayloadRows(
    pythonPath,
    database,
    "SELECT json_object('revision',revision,'job',json(payload_json),'evidence',(SELECT json(payload_json) FROM media_execution_evidence WHERE job_id=?),'receipt_count',(SELECT count(*) FROM media_execution_receipts WHERE job_id=?),'step_receipt_count',(SELECT count(*) FROM media_recipe_step_receipts WHERE job_id=?)) FROM job_store WHERE job_id=?",
    [jobId, jobId, jobId, jobId],
  );
  if (rows.length !== 1) throw new Error("binary revocation Job authority is missing");
  return rows[0];
}

function binaryStagingFiles(temporaryRoot) {
  const root = path.join(temporaryRoot, "vault", ".rebuild-data", "media-hands");
  if (!fs.existsSync(root)) return [];
  const files = [];
  const visit = (directory) => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const file = path.join(directory, entry.name);
      if (entry.isDirectory()) visit(file);
      else if (entry.isFile() && /(?:[.](?:jpe?g|png|webp)|[.]download[.]part)$/i.test(entry.name)) files.push(file);
    }
  };
  visit(root);
  return files;
}

function documentAuthorityCount(temporaryRoot, pythonPath) {
  const database = path.join(temporaryRoot, "vault", ".rebuild-data", "jobs.sqlite3");
  const rows = sqlitePayloadRows(
    pythonPath,
    database,
    "SELECT json_object('count',count(*)) FROM crp_structured_records WHERE collection='documents'",
    [],
  );
  const jsonRoot = path.join(temporaryRoot, "vault", ".rebuild-data", "objects", "default", "documents");
  const jsonCount = fs.existsSync(jsonRoot)
    ? fs.readdirSync(jsonRoot).filter((name) => name.endsWith(".json") && !name.endsWith(".meta.json")).length
    : 0;
  return Number(rows[0]?.count || 0) + jsonCount;
}

function inspectControlledOcrAuthority(temporaryRoot, pythonPath, jobId) {
  const database = path.join(temporaryRoot, "vault", ".rebuild-data", "jobs.sqlite3");
  return {
    job: inspectBinaryRevocationAuthority(temporaryRoot, pythonPath, jobId),
    receipts: sqlitePayloadRows(
      pythonPath, database,
      "SELECT payload_json FROM media_execution_receipts WHERE job_id=? ORDER BY job_id",
      [jobId],
    ),
    step_receipts: sqlitePayloadRows(
      pythonPath, database,
      "SELECT payload_json FROM media_recipe_step_receipts WHERE job_id=? ORDER BY step_name",
      [jobId],
    ),
  };
}

async function inspectControlledOcrDocument(page, documentId) {
  return requireStatus(
    await rendererApi(page, `/api/rebuild/documents/${encodeURIComponent(documentId)}`),
    200,
    "controlled OCR Document authority",
  );
}

async function runXhsControlledBundledOcrGate(session, temporaryRoot, pythonPath) {
  requireStatus(await rendererApi(session.page, "/api/ai/media-ingress-selection/revisions", {
    method: "POST",
    body: { command_id: "select-xhs-controlled-ocr-hands-0001", expected_revision: 0, confirm: true, mode: "hands" },
  }), 200, "controlled OCR Hands selection");
  const granted = await operateVisiblePanel(session.page, "grant");
  if (granted.state !== "已授权") throw new Error("visible controlled OCR credential grant did not become active");
  const diagnostic = { pythonPath, temporaryRoot };
  const resolved = await submitAndApproveControlledResolve(session.page, {
    kind: "text", text: FIXTURE_URL, source_ref: null,
  }, diagnostic);
  const frozen = controlledResolveEvidence(temporaryRoot, pythonPath, resolved.turnId);
  assertGovernedTurnEvidence(temporaryRoot, pythonPath, resolved.turnId);
  const permission = requireStatus(await rendererApi(
    session.page,
    `/api/ai/projects/${encodeURIComponent(PROJECT_ID)}/source-permissions/grant`,
    {
      method: "POST",
      body: {
        command_id: `grant-xhs-ocr-${randomUUID().replaceAll("-", "")}`,
        manifest_ref: frozen.manifestRef,
        expected_permission_revision: 0,
        confirm: true,
      },
    },
  ), 200, "controlled OCR SourcePermission grant");
  if (permission.state !== "granted" || permission.source_manifest_ref !== frozen.manifestRef || permission.permission_revision !== 1) {
    throw new Error(`controlled OCR SourcePermission did not bind the frozen Manifest: ${JSON.stringify(permission)}`);
  }
  const admitted = await submitAndApproveControlledResolve(session.page, {
    kind: "source_ref", text: null, source_ref: frozen.manifestRef,
  }, diagnostic);
  const job = admittedResolveEvidence(temporaryRoot, pythonPath, admitted.turnId);
  const arrivedPath = path.join(temporaryRoot, "vault", BINARY_ARRIVED_RELATIVE_PATH);
  const releasePath = path.join(temporaryRoot, "vault", BINARY_RELEASE_RELATIVE_PATH);
  const arrived = await waitFor(() => {
    if (!fs.existsSync(arrivedPath)) throw new Error("controlled OCR binary wire has not arrived");
    return JSON.parse(fs.readFileSync(arrivedPath, "utf8"));
  }, "controlled OCR binary wire arrival", 30000);
  fs.writeFileSync(releasePath, "release\n", "utf8");
  const authority = await waitFor(() => {
    const value = inspectControlledOcrAuthority(temporaryRoot, pythonPath, job.jobId);
    if (value.job.job?.status === "failed") {
      throw Object.assign(new Error(`controlled OCR Job failed: ${JSON.stringify(value.job.job.error)}`), { fatal: true });
    }
    if (value.job.job?.status !== "completed") throw new Error("controlled OCR Job is not completed");
    return value;
  }, "controlled OCR Job completion", 60000);
  const published = authority.job.job?.published_outputs || [];
  const documentId = published[0]?.object_id;
  const document = typeof documentId === "string"
    ? await inspectControlledOcrDocument(session.page, documentId)
    : null;
  const markdown = String(document?.markdown || "");
  const normalizedOcr = markdown.toUpperCase().replace(/[^A-Z0-9]/g, "");
  if (
    authority.job.evidence?.state !== "completed"
    || authority.receipts.length !== 1
    || authority.step_receipts.length !== 2
    || published.length !== 1
    || published[0]?.kind !== "document"
    || published[0]?.published !== true
    || !document
    || !normalizedOcr.includes("PROJECTMEMORY2026")
  ) {
    throw new Error(`controlled OCR authority mismatch: ${JSON.stringify({ job: authority.job, receipt_count: authority.receipts.length, step_receipt_count: authority.step_receipts.length, published, document, normalized_ocr: normalizedOcr.slice(0, 200) })}`);
  }
  const serialized = JSON.stringify({ frozen: frozen.receipt, permission, authority });
  if (serialized.includes(FIXTURE_COOKIE_CANARY)) throw new Error("controlled OCR authority leaked the Cookie");
  assertNoOutputCanary(session.childOutput, "controlled OCR Electron or sidecar output");
  return {
    electron_pid: session.child.pid,
    sidecar_pid: session.sidecarPid,
    job_id: job.jobId,
    document,
    job_revision: authority.job.revision,
    document_id: published[0].object_id,
    permission_revision: permission.permission_revision,
    binary_wire_count: arrived.count,
    media_receipt_count: authority.receipts.length,
    recipe_receipt_count: authority.step_receipts.length,
    ocr_provider_revision: authority.step_receipts.find((item) => item.step_name === "ocr_document")?.provider_revision || null,
    real_ocr_text_verified: true,
  };
}

async function runXhsControlledBinaryRevocationGate(session, temporaryRoot, pythonPath) {
  requireStatus(await rendererApi(session.page, "/api/ai/media-ingress-selection/revisions", {
    method: "POST",
    body: { command_id: "select-xhs-controlled-binary-hands-0001", expected_revision: 0, confirm: true, mode: "hands" },
  }), 200, "controlled binary Hands selection");
  const granted = await operateVisiblePanel(session.page, "grant");
  if (granted.state !== "已授权") throw new Error("visible controlled credential grant did not become active");
  const diagnostic = { pythonPath, temporaryRoot };
  const resolved = await submitAndApproveControlledResolve(session.page, {
    kind: "text", text: FIXTURE_URL, source_ref: null,
  }, diagnostic);
  const frozen = controlledResolveEvidence(temporaryRoot, pythonPath, resolved.turnId);
  assertGovernedTurnEvidence(temporaryRoot, pythonPath, resolved.turnId);
  const permission = requireStatus(await rendererApi(
    session.page,
    `/api/ai/projects/${encodeURIComponent(PROJECT_ID)}/source-permissions/grant`,
    {
      method: "POST",
      body: {
        command_id: `grant-xhs-binary-${randomUUID().replaceAll("-", "")}`,
        manifest_ref: frozen.manifestRef,
        expected_permission_revision: 0,
        confirm: true,
      },
    },
  ), 200, "controlled frozen Manifest SourcePermission grant");
  if (permission.state !== "granted" || permission.source_manifest_ref !== frozen.manifestRef || permission.permission_revision !== 1) {
    throw new Error(`SourcePermission did not bind the frozen controlled Manifest: ${JSON.stringify(permission)}`);
  }
  const admitted = await submitAndApproveControlledResolve(session.page, {
    kind: "source_ref", text: null, source_ref: frozen.manifestRef,
  }, diagnostic);
  const job = admittedResolveEvidence(temporaryRoot, pythonPath, admitted.turnId);
  const arrivedPath = path.join(temporaryRoot, "vault", BINARY_ARRIVED_RELATIVE_PATH);
  const releasePath = path.join(temporaryRoot, "vault", BINARY_RELEASE_RELATIVE_PATH);
  const arrived = await waitFor(() => {
    if (!fs.existsSync(arrivedPath)) {
      const authority = inspectBinaryRevocationAuthority(temporaryRoot, pythonPath, job.jobId);
      if (["failed", "cancelled", "completed"].includes(authority.job?.status)) {
        const tracePath = path.join(temporaryRoot, "vault", BINARY_TRACE_RELATIVE_PATH);
        const trace = fs.existsSync(tracePath) ? JSON.parse(fs.readFileSync(tracePath, "utf8")) : null;
        throw new Error(`controlled binary Job became terminal before wire arrival: ${JSON.stringify({ authority, trace })}`);
      }
      throw new Error("controlled binary wire has not reached the fixture boundary");
    }
    const value = JSON.parse(fs.readFileSync(arrivedPath, "utf8"));
    if (value?.schema_version !== "1.0.0" || value?.count !== 1 || !/^asset-[12][.]jpg$/.test(String(value?.target_basename))) {
      throw new Error(`controlled binary arrival marker is invalid: ${JSON.stringify(value)}`);
    }
    return value;
  }, "controlled binary wire arrival", 30000);
  const revoked = await operateVisiblePanel(session.page, "revoke");
  if (revoked.state !== "已撤销") throw new Error("visible binary-boundary revoke did not become inactive");
  fs.writeFileSync(releasePath, "release\n", "utf8");
  const authority = await waitFor(() => {
    const value = inspectBinaryRevocationAuthority(temporaryRoot, pythonPath, job.jobId);
    if (value.job?.status !== "failed") throw new Error(`binary revocation Job is not terminal: ${JSON.stringify(value.job)}`);
    return value;
  }, "controlled binary revocation Job terminal", 30000);
  if (
    authority.job?.error?.code !== "media.needs_reconcile"
    || authority.job?.error?.retryable !== false
    || authority.evidence?.state !== "unknown_effect"
    || authority.receipt_count !== 0
    || authority.step_receipt_count !== 0
    || (authority.job?.published_outputs || []).length !== 0
    || (authority.job?.staged_outputs || []).length !== 0
  ) {
    throw new Error(`controlled binary revocation authority did not preserve unknown-effect semantics: ${JSON.stringify(authority)}`);
  }
  const stagingFiles = binaryStagingFiles(temporaryRoot);
  const documentCount = documentAuthorityCount(temporaryRoot, pythonPath);
  if (stagingFiles.length !== 0 || documentCount !== 0) {
    throw new Error(`controlled binary revocation left durable output: ${JSON.stringify({ staging_basenames: stagingFiles.map((file) => path.basename(file)), document_count: documentCount })}`);
  }
  const serialized = JSON.stringify({ frozen: frozen.receipt, permission, authority, arrived });
  if (serialized.includes(FIXTURE_COOKIE_CANARY)) throw new Error("controlled binary authority evidence leaked the Cookie");
  assertNoOutputCanary(session.childOutput, "binary-revocation Electron or sidecar output");
  return {
    electron_pid: session.child.pid,
    sidecar_pid: session.sidecarPid,
    job_id: job.jobId,
    job_revision: authority.revision,
    job_status: authority.job.status,
    execution_state: authority.evidence.state,
    permission_revision: permission.permission_revision,
    binary_wire_count: arrived.count,
    binary_target_basename: arrived.target_basename,
    media_receipt_count: authority.receipt_count,
    recipe_receipt_count: authority.step_receipt_count,
    staged_asset_count: 0,
    document_count: 0,
    visible_revoke_at_binary_boundary: true,
    frozen_source_ref_execution_covered: true,
  };
}

module.exports = {
  assertNoOutputCanary,
  assertRevokedControlledResolve,
  assertSecretIsolation,
  inspectBinaryRevocationAuthority,
  inspectControlledOcrAuthority,
  inspectControlledOcrDocument,
  runXhsControlledBundledOcrGate,
  runXhsControlledBinaryRevocationGate,
  runXhsControlledCredentialElectronGate,
};
