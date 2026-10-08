const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const { createFileGrant, uploadFileGrant } = require("../file-grant.cjs");
const { currentSessionForRequest } = require("../desktop-session.cjs");

const MAX_IMAGE_BYTES = 2 * 1024 * 1024;
const SESSION_TTL_MS = 2 * 60 * 1000;

class CompanionScreenVisionController {
  constructor({ desktopCapturer, nativeImage, tempRoot, sessionProvider, fetchFn = fetch, createFileGrantFn = createFileGrant, uploadFileGrantFn = uploadFileGrant, now = Date.now }) {
    if (!desktopCapturer?.getSources || !nativeImage?.createFromBuffer || typeof sessionProvider !== "function" || typeof fetchFn !== "function" || typeof createFileGrantFn !== "function" || typeof uploadFileGrantFn !== "function") throw new TypeError("screen vision dependencies are invalid");
    this.desktopCapturer = desktopCapturer; this.nativeImage = nativeImage; this.tempRoot = path.resolve(tempRoot); this.sessionProvider = sessionProvider; this.fetchFn = fetchFn; this.createFileGrantFn = createFileGrantFn; this.uploadFileGrantFn = uploadFileGrantFn; this.now = now;
    this.selection = null; this.capture = null; this.active = null; this.operationVersion = 0;
    cleanOwnedTempFiles(this.tempRoot);
  }

  async listSources() {
    this.cancel();
    const operationVersion = this.operationVersion;
    const sources = await this.desktopCapturer.getSources({ types: ["screen", "window"], thumbnailSize: { width: 480, height: 300 }, fetchWindowIcons: false });
    if (this.operationVersion !== operationVersion) throw new Error("screen_vision_cancelled");
    const sessionId = crypto.randomUUID(); const expiresAt = this.now() + SESSION_TTL_MS; const records = new Map(); const items = [];
    for (const source of sources.slice(0, 32)) {
      if (!source?.id || !source.thumbnail || source.thumbnail.isEmpty?.()) continue;
      const itemId = crypto.randomUUID(); const preview = source.thumbnail.toDataURL();
      if (typeof preview !== "string" || preview.length > 800_000 || !preview.startsWith("data:image/png;base64,")) continue;
      records.set(itemId, source.id);
      items.push(Object.freeze({ item_id: itemId, kind: String(source.id).startsWith("screen:") ? "screen" : "window", name: safeName(source.name), preview }));
    }
    this.selection = { sessionId, expiresAt, records };
    return Object.freeze({ session_id: sessionId, expires_at_ms: expiresAt, items: Object.freeze(items) });
  }

  async captureSource(payload) {
    const selection = this.selection;
    if (!selection || payload?.session_id !== selection.sessionId || selection.expiresAt <= this.now()) throw new Error("screen_vision_selection_expired");
    const sourceId = selection.records.get(payload?.item_id);
    if (!sourceId) throw new Error("screen_vision_source_invalid");
    const sources = await this.desktopCapturer.getSources({ types: [String(sourceId).startsWith("screen:") ? "screen" : "window"], thumbnailSize: { width: 1600, height: 1600 }, fetchWindowIcons: false });
    if (this.selection !== selection) throw new Error("screen_vision_cancelled");
    const source = sources.find((item) => item.id === sourceId);
    if (!source?.thumbnail || source.thumbnail.isEmpty?.()) throw new Error("screen_vision_capture_unavailable");
    const image = fitImage(source.thumbnail, 1600);
    const bytes = boundedJpeg(image);
    const size = image.getSize(); const captureId = crypto.randomUUID();
    this.capture = { captureId, expiresAt: this.now() + SESSION_TTL_MS, width: size.width, height: size.height };
    this.selection = null;
    return Object.freeze({ capture_id: captureId, width: size.width, height: size.height, media_type: "image/jpeg", bytes });
  }

  async confirm(payload) {
    const record = this.capture;
    if (!record || payload?.capture_id !== record.captureId || record.expiresAt <= this.now()) throw new Error("screen_vision_capture_expired");
    const question = requireQuestion(payload?.question); const bytes = requireJpeg(payload?.bytes, this.nativeImage, record);
    const initialSession = this.sessionProvider();
    const active = initialSession ? { ...initialSession } : null;
    if (!active?.origin || !active?.secret || !active?.instance_id) throw new Error("screen_vision_backend_unavailable");
    // Validation and backend discovery failures are retryable. The capture is
    // consumed only when an outbound operation is actually about to begin.
    this.capture = null;
    this.cancelActive(); const controller = new AbortController(); this.active = controller;
    fs.mkdirSync(this.tempRoot, { recursive: true });
    const filePath = path.join(this.tempRoot, `screen-${crypto.randomUUID()}.jpg`);
    fs.writeFileSync(filePath, bytes, { flag: "wx", mode: 0o600 });
    let grantId = null;
    try {
      const grant = await this.createFileGrantFn({ filePath, session: active, mediaType: "image/jpeg", sourceKind: "image" });
      const bounded = AbortSignal.any([controller.signal, AbortSignal.timeout(95_000)]);
      const issued = await this.uploadFileGrantFn(grant, currentSessionForRequest(this.sessionProvider, active), { signal: bounded, endpointPath: "/api/rebuild/companion/vision/grants" });
      grantId = issued?.grant?.grant_id;
      if (typeof grantId !== "string") throw new Error("screen_vision_grant_response_invalid");
      const projectId = requireProjectId(payload?.project_id);
      const turn = visionTurnRequest({ projectId, grantId, question });
      const submitted = await postJson(this.fetchFn, currentSessionForRequest(this.sessionProvider, active), "/api/ai/turns", turn, bounded);
      if (submitted.status !== "waiting_approval") throw new Error("screen_vision_turn_approval_missing");
      const pending = await getJson(this.fetchFn, currentSessionForRequest(this.sessionProvider, active), `/api/ai/turns/${encodeURIComponent(turn.turn_id)}/events`, bounded);
      const approval = [...(pending.events || [])].reverse().find((event) => event?.type === "approval.required");
      if (!approval?.event_id) throw new Error("screen_vision_turn_approval_missing");
      const completed = await postJson(this.fetchFn, currentSessionForRequest(this.sessionProvider, active), `/api/ai/turns/${encodeURIComponent(turn.turn_id)}/actions`, visionApprovalAction(turn, submitted, approval), bounded);
      if (completed.status !== "completed") throw new Error("screen_vision_turn_incomplete");
      const outcome = await getJson(this.fetchFn, currentSessionForRequest(this.sessionProvider, active), `/api/ai/turns/${encodeURIComponent(turn.turn_id)}/events`, bounded);
      const content = outcome?.presentation?.content;
      if (!content || typeof content.text !== "string") throw new Error("screen_vision_turn_result_invalid");
      grantId = null;
      return Object.freeze(content);
    } finally {
      if (grantId) await revokeGrantBestEffort(this.fetchFn, this.sessionProvider, active, grantId);
      fs.rmSync(filePath, { force: true });
      if (this.active === controller) this.active = null;
    }
  }

  cancel() { this.operationVersion += 1; this.selection = null; this.capture = null; return this.cancelActive(); }
  cancelActive() { if (!this.active) return false; this.active.abort(new Error("screen_vision_cancelled")); this.active = null; return true; }
}

function safeName(value) { return typeof value === "string" ? value.replace(/[\x00-\x1f\x7f]/g, " ").replace(/\s+/g, " ").trim().slice(0, 120) || "未命名目标" : "未命名目标"; }
function fitImage(image, maximum) { const size=image.getSize();if(size.width<=maximum&&size.height<=maximum)return image;const ratio=Math.min(maximum/size.width,maximum/size.height);return image.resize({width:Math.max(1,Math.round(size.width*ratio)),height:Math.max(1,Math.round(size.height*ratio)),quality:"best"}); }
function boundedJpeg(image) { for(const quality of [82,72,62,52,42,32]){const bytes=image.toJPEG(quality);if(bytes.length<=MAX_IMAGE_BYTES)return bytes;}throw new Error("screen_vision_image_too_large"); }
function requireQuestion(value) { if(typeof value!=="string"||!value.trim()||value.length>2000||/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(value))throw new Error("screen_vision_question_invalid");return value.trim(); }
function requireProjectId(value) { const projectId=typeof value==="string"&&value.trim()?value.trim():"default";if(projectId.length>191||/[\x00-\x1f\x7f]/.test(projectId))throw new Error("screen_vision_project_invalid");return projectId; }
function requireJpeg(value,nativeImage,record) { const bytes=Buffer.from(value instanceof Uint8Array?value:[]);if(bytes.length<16||bytes.length>MAX_IMAGE_BYTES||bytes[0]!==0xff||bytes[1]!==0xd8||bytes[2]!==0xff)throw new Error("screen_vision_image_invalid");const image=nativeImage.createFromBuffer(bytes);if(image.isEmpty())throw new Error("screen_vision_image_invalid");const size=image.getSize();if(size.width<1||size.height<1||size.width>1600||size.height>1600||size.width!==record.width||size.height!==record.height)throw new Error("screen_vision_image_dimensions_invalid");return bytes; }
function visionTurnRequest({projectId,grantId,question}){const digest=crypto.createHash("sha256").update(`${projectId}\0${grantId}\0${question}`).digest("hex").slice(0,32);return{schema_version:"1.0.0",turn_id:`turn-${digest}`,session_id:`session-companion-${digest}`,operation_id:`vision:${digest}`,idempotency_key:`companion-vision-${digest}`,scope:{kind:"project",project_id:projectId,series_id:null},input:{kind:"text",text:question,refs:[{kind:"companion_vision_grant",object_id:grantId,uri:`crp://default/companion/vision/grants/${grantId}`}]},desired_outcome:"companion.vision.analyze",privacy:{mode:"remote_allowed",allow_remote:true,pii:"possible",consent_refs:["crp://default/consent/provider-egress-policy"],retention:"local_durable"},capability_policy:{allowed:["companion.vision.context.read","companion.vision.analyze.write"],denied:[],require_approval:["companion.vision.analyze.write"]},context_policy:{include_project_skill:true,include_memory:false,include_session_history:false,max_context_bytes:262144},approval_policy:{mode:"explicit",auto_approve_read_only:true},created_at:"2026-08-23T00:00:00+00:00"};}
function visionApprovalAction(turn,receipt,approval){const digest=turn.turn_id.slice(5);return{schema_version:"1.0.0",action_id:`action-${digest}`,turn_id:turn.turn_id,type:"approve",target_event_id:approval.event_id,reason:"user approved the masked Companion Vision preview",actor:"user",expected_sequence:receipt.current_sequence,idempotency_key:`approve-companion-vision-${digest}`,created_at:"2026-08-23T00:00:00+00:00"};}
async function postJson(fetchFn,session,endpoint,body,signal){const response=await fetchFn(`${session.origin}${endpoint}`,{method:"POST",headers:{Accept:"application/json","Content-Type":"application/json","X-Chriptmas-Desktop-Session":session.secret},body:JSON.stringify(body),signal});const result=await response.json().catch(()=>null);if(!response.ok||!result)throw new Error(`screen_vision_ai_api_failed_${response.status}`);return result;}
async function getJson(fetchFn,session,endpoint,signal){const response=await fetchFn(`${session.origin}${endpoint}`,{method:"GET",headers:{Accept:"application/json","X-Chriptmas-Desktop-Session":session.secret},signal});const result=await response.json().catch(()=>null);if(!response.ok||!result)throw new Error(`screen_vision_ai_api_failed_${response.status}`);return result;}
async function revokeGrantBestEffort(fetchFn,sessionProvider,startingSession,grantId){try{const session=currentSessionForRequest(sessionProvider,startingSession);await fetchFn(`${session.origin}/api/rebuild/companion/vision/grants/${encodeURIComponent(grantId)}`,{method:"DELETE",headers:{Accept:"application/json","X-Chriptmas-Desktop-Session":session.secret},signal:AbortSignal.timeout(2000)});}catch{}}
function cleanOwnedTempFiles(root){fs.mkdirSync(root,{recursive:true});const rootInfo=fs.lstatSync(root);if(!rootInfo.isDirectory()||rootInfo.isSymbolicLink())throw new Error("screen_vision_temp_root_unsafe");for(const name of fs.readdirSync(root)){if(!/^screen-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.jpg$/.test(name))continue;const target=path.join(root,name);const info=fs.lstatSync(target);if(info.isFile()&&!info.isSymbolicLink())fs.rmSync(target,{force:true});}}

module.exports = { CompanionScreenVisionController, MAX_IMAGE_BYTES, boundedJpeg, cleanOwnedTempFiles, requireJpeg, requireProjectId, safeName, visionApprovalAction, visionTurnRequest };
