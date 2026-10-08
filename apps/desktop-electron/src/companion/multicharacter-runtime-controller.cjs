const ACTION_MESSAGES = Object.freeze({
  wave: Object.freeze({ type: "emote", emote: "wave" }),
  greeting: Object.freeze({ type: "short_line", text: "你好呀，今天也请多关照。" }),
  cheer: Object.freeze({ type: "short_line", text: "一起加油，把今天过好吧。" }),
});

const UNAVAILABLE_PROJECTION = Object.freeze({
  state: "unavailable",
  enabled: false,
  consented: false,
  character_id: "chriptmas.bear",
  allowed_character_ids: Object.freeze([]),
  revision: 0,
  peers: Object.freeze([]),
});

class CompanionMultiCharacterRuntimeController {
  constructor({ createSettings, createLink }) {
    if (typeof createSettings !== "function" || typeof createLink !== "function") {
      throw new TypeError("companion_multicharacter_runtime_boundary_invalid");
    }
    this.createSettings = createSettings;
    this.createLink = createLink;
    this.settings = null;
    this.link = null;
  }

  async initialize() {
    if (!this.settings) this.settings = this.createSettings();
    const settings = this.settings.read();
    if (settings.enabled) await this.replaceLink(settings);
    else this.createLink(settings).cleanup();
    return this.projection();
  }

  projection() {
    const settings = this.settings?.read();
    if (!settings) return UNAVAILABLE_PROJECTION;
    const peers = (this.link?.peers() || []).map((peer) => Object.freeze({
      instance_id: peer.instance_id,
      character_id: peer.character_id,
      state: peer.state,
      compatible: peer.protocol_version === 1,
    }));
    return Object.freeze({ state: "ready", ...settings, peers: Object.freeze(peers) });
  }

  async configure(payload) {
    if (!this.settings) throw new Error("companion_multicharacter_unavailable");
    const settings = this.settings.save({
      enabled: payload.enabled,
      consented: payload.consented,
      characterId: payload.character_id,
      allowedCharacterIds: payload.allowed_character_ids,
      expectedRevision: payload.expected_revision,
    });
    await this.replaceLink(settings);
    return this.projection();
  }

  async sendAction(instanceId, action) {
    const message = ACTION_MESSAGES[action];
    if (!message) throw new Error("companion_multicharacter_payload_rejected");
    if (!this.link) throw new Error("companion_multicharacter_unavailable");
    const result = await this.link.send(instanceId, message);
    return Object.freeze({ status: result.status });
  }

  async replaceLink(settings) {
    const previous = this.link;
    this.link = null;
    await previous?.stop();
    if (!settings.enabled) return;
    const next = this.createLink(settings);
    try {
      await next.start();
      this.link = next;
    } catch (error) {
      try { await next.stop(); } catch {}
      throw error;
    }
  }

  async stop() {
    const current = this.link;
    this.link = null;
    if (current) await current.stop();
  }
}

module.exports = { ACTION_MESSAGES, CompanionMultiCharacterRuntimeController, UNAVAILABLE_PROJECTION };
