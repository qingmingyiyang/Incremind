const CHANNELS = Object.freeze([
  "chriptmas:companion-microphone-arm",
  "chriptmas:companion-voice-call-transcribe",
  "chriptmas:companion-voice-call-present",
  "chriptmas:companion-voice-call-cancel",
]);

const PLAYBACK_CHANNEL = "chriptmas:companion-voice-playback-status";
const PLAYBACK_STATUSES = Object.freeze(["playing", "ended", "failed", "cancelled"]);

class CompanionVoiceCallIpcController {
  constructor({
    ipcMain,
    requireMainRenderer,
    mainWindowProvider,
    petWindowProvider,
    microphonePermission,
    voiceProvider,
    voiceCallProvider,
    presentationProvider,
    cancelAudio,
    presentReply,
  }) {
    if (!ipcMain || typeof ipcMain.handle !== "function" || typeof ipcMain.removeHandler !== "function"
      || typeof ipcMain.on !== "function" || typeof ipcMain.removeListener !== "function") {
      throw new TypeError("companion_voice_call_ipc_invalid");
    }
    if (![requireMainRenderer, mainWindowProvider, petWindowProvider, voiceProvider, voiceCallProvider,
      presentationProvider, cancelAudio, presentReply].every((value) => typeof value === "function")
      || !microphonePermission || typeof microphonePermission.arm !== "function"
      || typeof microphonePermission.clear !== "function") {
      throw new TypeError("companion_voice_call_boundary_invalid");
    }
    this.ipcMain = ipcMain;
    this.requireMainRenderer = requireMainRenderer;
    this.mainWindowProvider = mainWindowProvider;
    this.petWindowProvider = petWindowProvider;
    this.microphonePermission = microphonePermission;
    this.voiceProvider = voiceProvider;
    this.voiceCallProvider = voiceCallProvider;
    this.presentationProvider = presentationProvider;
    this.cancelAudio = cancelAudio;
    this.presentReply = presentReply;
    this.playbackListener = (event, payload) => this.playbackStatus(event, payload);
    this.installed = false;
  }

  install() {
    if (this.installed) return false;
    const handlers = new Map([
      [CHANNELS[0], (event, payload) => this.arm(event, payload)],
      [CHANNELS[1], (event, payload) => this.transcribe(event, payload)],
      [CHANNELS[2], (event, payload) => this.present(event, payload)],
      [CHANNELS[3], (event) => this.cancel(event)],
    ]);
    const registered = [];
    try {
      for (const [channel, handler] of handlers) {
        this.ipcMain.handle(channel, handler);
        registered.push(channel);
      }
      this.ipcMain.on(PLAYBACK_CHANNEL, this.playbackListener);
    } catch (error) {
      this.ipcMain.removeListener(PLAYBACK_CHANNEL, this.playbackListener);
      for (const channel of registered) this.ipcMain.removeHandler(channel);
      throw error;
    }
    this.installed = true;
    return true;
  }

  arm(event, payload) {
    this.requireMainRenderer(event);
    if (!isExactObject(payload, ["transient_activation"]) || payload.transient_activation !== true) {
      throw new Error("companion_microphone_activation_required");
    }
    this.voiceProvider()?.cancel();
    this.cancelAudio();
    const mainWindow = this.mainWindowProvider();
    return this.microphonePermission.arm(event.sender, {
      purpose: "companion-voice",
      transientActivation: true,
      topFrame: event.senderFrame === mainWindow?.webContents?.mainFrame,
    });
  }

  transcribe(event, payload) {
    this.requireMainRenderer(event);
    const voiceCall = this.voiceCallProvider();
    if (!voiceCall) throw new Error("companion_voice_call_unavailable");
    this.microphonePermission.clear();
    return voiceCall.transcribe(payload);
  }

  present(event, payload) {
    this.requireMainRenderer(event);
    const presentation = this.presentationProvider();
    if (!presentation || !isExactObject(payload, ["request_id", "text"])) {
      throw new Error("companion_voice_call_present_rejected");
    }
    this.presentReply(payload.text, { speak: false });
    return presentation.present(payload);
  }

  cancel(event) {
    this.requireMainRenderer(event);
    this.microphonePermission.clear();
    this.voiceProvider()?.cancel();
    this.cancelAudio();
    this.presentationProvider()?.cancel();
    return Object.freeze({ cancelled: this.voiceCallProvider()?.cancel() === true });
  }

  playbackStatus(event, payload) {
    const petWindow = this.petWindowProvider();
    if (!petWindow || petWindow.isDestroyed() || event?.sender?.id !== petWindow.webContents?.id) return false;
    if (!payload || typeof payload.request_id !== "string" || !PLAYBACK_STATUSES.includes(payload.status)) return false;
    return this.presentationProvider()?.acknowledge(payload.request_id, payload.status) === true;
  }

  dispose() {
    if (!this.installed) return false;
    this.ipcMain.removeListener(PLAYBACK_CHANNEL, this.playbackListener);
    for (const channel of CHANNELS) this.ipcMain.removeHandler(channel);
    this.installed = false;
    return true;
  }
}

function isExactObject(value, keys) {
  return Boolean(value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).sort().join() === [...keys].sort().join());
}

module.exports = { CHANNELS, PLAYBACK_CHANNEL, PLAYBACK_STATUSES, CompanionVoiceCallIpcController };
