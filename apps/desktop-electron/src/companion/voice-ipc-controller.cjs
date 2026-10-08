const CHANNELS = Object.freeze([
  "chriptmas:companion-voice-status",
  "chriptmas:companion-voice-configure",
  "chriptmas:companion-voice-reference-select",
  "chriptmas:companion-voice-test",
]);

const CONFIG_KEYS = Object.freeze(["enabled", "origin", "prompt_lang", "prompt_text", "text_lang"]);
const TEST_PHRASE = "你好，我是你的桌面伙伴。语音连接测试成功。";

function requireVoiceConfig(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)
    || Object.keys(payload).sort().join() !== CONFIG_KEYS.join()) {
    throw new Error("companion_voice_payload_rejected");
  }
  return payload;
}

class CompanionVoiceIpcController {
  constructor({ ipcMain, dialog, requireMainRenderer, mainWindowProvider, voiceProvider, isQuiet, dispatchAudio }) {
    if (!ipcMain || typeof ipcMain.handle !== "function" || typeof ipcMain.removeHandler !== "function") {
      throw new TypeError("companion_voice_ipc_invalid");
    }
    if (!dialog || typeof dialog.showOpenDialog !== "function") {
      throw new TypeError("companion_voice_dialog_invalid");
    }
    if (typeof requireMainRenderer !== "function" || typeof mainWindowProvider !== "function"
      || typeof voiceProvider !== "function" || typeof isQuiet !== "function" || typeof dispatchAudio !== "function") {
      throw new TypeError("companion_voice_boundary_invalid");
    }
    this.ipcMain = ipcMain;
    this.dialog = dialog;
    this.requireMainRenderer = requireMainRenderer;
    this.mainWindowProvider = mainWindowProvider;
    this.voiceProvider = voiceProvider;
    this.isQuiet = isQuiet;
    this.dispatchAudio = dispatchAudio;
    this.installed = false;
  }

  install() {
    if (this.installed) return false;
    const handlers = new Map([
      [CHANNELS[0], (event) => this.status(event)],
      [CHANNELS[1], (event, payload) => this.configure(event, payload)],
      [CHANNELS[2], (event) => this.selectReference(event)],
      [CHANNELS[3], (event) => this.testPlayback(event)],
    ]);
    const registered = [];
    try {
      for (const [channel, handler] of handlers) {
        this.ipcMain.handle(channel, handler);
        registered.push(channel);
      }
    } catch (error) {
      for (const channel of registered) this.ipcMain.removeHandler(channel);
      throw error;
    }
    this.installed = true;
    return true;
  }

  status(event) {
    this.requireMainRenderer(event);
    return this.available().status();
  }

  configure(event, payload) {
    this.requireMainRenderer(event);
    const config = requireVoiceConfig(payload);
    const voice = this.voiceProvider();
    if (!voice) throw new Error("companion_voice_payload_rejected");
    return voice.configure(config);
  }

  async selectReference(event) {
    this.requireMainRenderer(event);
    const voice = this.available();
    const selection = await this.dialog.showOpenDialog(this.mainWindowProvider(), {
      title: "选择 GPT-SoVITS 参考音频",
      properties: ["openFile"],
      filters: [{ name: "WAV audio", extensions: ["wav"] }],
    });
    if (selection.canceled || !Array.isArray(selection.filePaths) || selection.filePaths.length !== 1) {
      return { status: "cancelled", voice: voice.status() };
    }
    return { status: "selected", voice: voice.setReference(selection.filePaths[0]) };
  }

  async testPlayback(event) {
    this.requireMainRenderer(event);
    const voice = this.available();
    if (this.isQuiet()) throw new Error("companion_voice_quiet");
    const result = await voice.speak(TEST_PHRASE);
    if (!this.dispatchAudio(result.audio)) throw new Error("companion_voice_pet_unavailable");
    return { status: "playing" };
  }

  available() {
    const voice = this.voiceProvider();
    if (!voice) throw new Error("companion_voice_unavailable");
    return voice;
  }

  dispose() {
    if (!this.installed) return false;
    for (const channel of CHANNELS) this.ipcMain.removeHandler(channel);
    this.installed = false;
    return true;
  }
}

module.exports = { CHANNELS, CONFIG_KEYS, TEST_PHRASE, CompanionVoiceIpcController, requireVoiceConfig };
