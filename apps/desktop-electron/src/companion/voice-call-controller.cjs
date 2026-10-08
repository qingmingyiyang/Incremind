const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const { createFileGrant, uploadFileGrant } = require("../file-grant.cjs");
const { currentSessionForRequest } = require("../desktop-session.cjs");

const MAX_AUDIO_BYTES = 12 * 1024 * 1024;
const MEDIA_SUFFIX = Object.freeze({ "audio/webm": ".webm", "audio/ogg": ".ogg", "audio/wav": ".wav" });

class CompanionVoiceCallController {
  constructor({ tempRoot, sessionProvider, fetchFn = fetch, createFileGrantFn = createFileGrant, uploadFileGrantFn = uploadFileGrant }) {
    if (typeof sessionProvider !== "function" || typeof fetchFn !== "function" || typeof createFileGrantFn !== "function" || typeof uploadFileGrantFn !== "function") throw new TypeError("voice call dependencies are invalid");
    this.tempRoot = path.resolve(tempRoot); this.sessionProvider = sessionProvider; this.fetchFn = fetchFn; this.createFileGrantFn = createFileGrantFn; this.uploadFileGrantFn = uploadFileGrantFn; this.active = null;
    cleanOwnedVoiceFiles(this.tempRoot);
  }

  async transcribe(payload) {
    const requestId = requireRequestId(payload?.request_id); const mediaType = requireMediaType(payload?.media_type); const bytes = requireAudio(payload?.bytes, mediaType);
    const initialSession = this.sessionProvider();
    const active = initialSession ? { ...initialSession } : null;
    if (!active?.origin || !active?.secret || !active?.instance_id) throw new Error("companion_voice_backend_unavailable");
    this.cancel(); const controller = new AbortController(); this.active = controller;
    fs.mkdirSync(this.tempRoot, { recursive: true });
    const filePath = path.join(this.tempRoot, `voice-recording-${crypto.randomUUID()}${MEDIA_SUFFIX[mediaType]}`);
    fs.writeFileSync(filePath, bytes, { flag: "wx", mode: 0o600 });
    let grantId = null;
    try {
      const grant = await this.createFileGrantFn({ filePath, session: active, mediaType, sourceKind: "audio" });
      const bounded = AbortSignal.any([controller.signal, AbortSignal.timeout(125_000)]);
      const issued = await this.uploadFileGrantFn(grant, currentSessionForRequest(this.sessionProvider, active), { signal: bounded, endpointPath: "/api/rebuild/companion/voice/grants" });
      grantId = issued?.grant?.grant_id;
      if (typeof grantId !== "string") throw new Error("companion_voice_grant_invalid");
      const requestSession = currentSessionForRequest(this.sessionProvider, active);
      const response = await this.fetchFn(`${requestSession.origin}/api/rebuild/companion/voice/transcribe`, { method: "POST", headers: { Accept: "application/json", "Content-Type": "application/json", "X-Chriptmas-Desktop-Session": requestSession.secret }, body: JSON.stringify({ request_id: requestId, grant_id: grantId }), signal: bounded });
      const result = await response.json().catch(() => null);
      if (!response.ok || !result?.result?.text) throw new Error(`companion_voice_transcription_failed_${response.status}`);
      grantId = null;
      return Object.freeze(result.result);
    } finally {
      if (grantId) await revokeGrantBestEffort(this.fetchFn, this.sessionProvider, active, grantId);
      fs.rmSync(filePath, { force: true });
      if (this.active === controller) this.active = null;
    }
  }

  cancel() { if (!this.active) return false; this.active.abort(new Error("companion_voice_cancelled")); this.active = null; return true; }
}

class CompanionVoiceCallPresentationController {
  constructor({ voiceProvider, isQuiet, dispatchAudio, cancelAudio, setTimer = setTimeout, clearTimer = clearTimeout }) {
    if (![voiceProvider, isQuiet, dispatchAudio, cancelAudio].every((value) => typeof value === "function")) throw new TypeError("voice presentation dependencies are invalid");
    this.voiceProvider = voiceProvider; this.isQuiet = isQuiet; this.dispatchAudio = dispatchAudio; this.cancelAudio = cancelAudio; this.setTimer = setTimer; this.clearTimer = clearTimer; this.sequence = 0; this.pending = null;
  }
  async present(payload) {
    requireRequestId(payload?.request_id);
    const text = typeof payload?.text === "string" ? payload.text.trim() : "";
    if (!text || text.length > 4_000 || text.includes("\0")) throw new Error("companion_voice_reply_invalid");
    if (this.pending) { this.cancelAudio(); this.finishPending("failed", "cancelled"); }
    const sequence = ++this.sequence;
    if (this.isQuiet()) return Object.freeze({ status: "skipped", reason: "quiet" });
    const voice = this.voiceProvider();
    if (!voice?.status?.().enabled) return Object.freeze({ status: "skipped", reason: "disabled" });
    let result;
    try { result = await voice.speak(text); }
    catch (error) {
      if (sequence !== this.sequence || /superseded|cancelled|disabled/.test(String(error?.message || ""))) throw new Error("companion_voice_call_cancelled");
      throw new Error("companion_voice_call_tts_failed");
    }
    if (sequence !== this.sequence) throw new Error("companion_voice_call_cancelled");
    if (this.isQuiet()) return Object.freeze({ status: "skipped", reason: "quiet" });
    const playback = new Promise((resolve, reject) => {
      const timer = this.setTimer(() => {
        if (this.pending?.requestId === payload.request_id && this.pending?.sequence === sequence) this.finishPending("failed", "playback_timeout");
      }, 120_000);
      this.pending = { requestId: payload.request_id, sequence, resolve, reject, timer };
    });
    if (!this.dispatchAudio(result?.audio, payload.request_id)) this.finishPending("failed", "playback_failed");
    return playback;
  }
  acknowledge(requestId, status) {
    if (!this.pending || this.pending.requestId !== requestId || !["playing", "ended", "failed", "cancelled"].includes(status)) return false;
    if (status === "playing") return true;
    this.finishPending(status, status === "ended" ? null : status === "cancelled" ? "cancelled" : "playback_failed");
    return true;
  }
  finishPending(status, reason) {
    const pending = this.pending; this.pending = null;
    if (!pending) return;
    this.clearTimer(pending.timer);
    if (status === "ended") pending.resolve(Object.freeze({ status: "completed" }));
    else pending.reject(new Error(reason === "cancelled" ? "companion_voice_call_cancelled" : "companion_voice_call_tts_failed"));
  }
  cancel() { this.sequence += 1; this.voiceProvider()?.cancel?.(); this.cancelAudio(); this.finishPending("failed", "cancelled"); }
  supersedePlayback() { this.sequence += 1; this.finishPending("failed", "cancelled"); }
}

function requireRequestId(value) { if (typeof value !== "string" || !/^voice:[a-f0-9-]{8,120}$/.test(value)) throw new Error("companion_voice_request_invalid"); return value; }
function requireMediaType(value) { if (!Object.hasOwn(MEDIA_SUFFIX, value)) throw new Error("companion_voice_media_type_invalid"); return value; }
function requireAudio(value, mediaType) { const bytes=Buffer.from(value instanceof Uint8Array?value:[]);if(bytes.length<12||bytes.length>MAX_AUDIO_BYTES||!audioMagic(bytes,mediaType))throw new Error("companion_voice_audio_invalid");return bytes; }
function audioMagic(bytes,mediaType){return(mediaType==="audio/webm"&&bytes.subarray(0,4).equals(Buffer.from([0x1a,0x45,0xdf,0xa3])))||(mediaType==="audio/ogg"&&bytes.subarray(0,4).toString("ascii")==="OggS")||(mediaType==="audio/wav"&&bytes.subarray(0,4).toString("ascii")==="RIFF"&&bytes.subarray(8,12).toString("ascii")==="WAVE");}
async function revokeGrantBestEffort(fetchFn,sessionProvider,startingSession,grantId){try{const session=currentSessionForRequest(sessionProvider,startingSession);await fetchFn(`${session.origin}/api/rebuild/companion/voice/grants/${encodeURIComponent(grantId)}`,{method:"DELETE",headers:{Accept:"application/json","X-Chriptmas-Desktop-Session":session.secret},signal:AbortSignal.timeout(2000)});}catch{}}
function cleanOwnedVoiceFiles(root){fs.mkdirSync(root,{recursive:true});const info=fs.lstatSync(root);if(!info.isDirectory()||info.isSymbolicLink())throw new Error("companion_voice_temp_root_unsafe");for(const name of fs.readdirSync(root)){if(!/^voice-recording-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.(?:webm|ogg|wav)$/.test(name))continue;const target=path.join(root,name);const item=fs.lstatSync(target);if(item.isFile()&&!item.isSymbolicLink())fs.rmSync(target,{force:true});}}

module.exports = { CompanionVoiceCallController, CompanionVoiceCallPresentationController, MAX_AUDIO_BYTES, audioMagic, cleanOwnedVoiceFiles, requireAudio };
