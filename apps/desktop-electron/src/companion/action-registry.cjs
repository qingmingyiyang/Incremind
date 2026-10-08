const ACTION_ID = /^[a-z][a-z0-9]*(?:[._-][a-z0-9]+){1,7}$/;
const SOURCES = new Set(["pet", "overlay", "main", "tray", "system"]);

class CompanionActionRegistry {
  constructor(definitions) {
    if (!Array.isArray(definitions) || definitions.length === 0) throw new TypeError("companion action definitions are required");
    this.definitions = new Map();
    for (const definition of definitions) this.register(definition);
  }

  register(definition) {
    if (!definition || typeof definition !== "object" || Array.isArray(definition)) throw new TypeError("companion action definition is invalid");
    const allowedKeys = new Set(["id", "label", "sources", "handler", "validatePayload", "enabled", "menu"]);
    if (Object.keys(definition).some((key) => !allowedKeys.has(key))) throw new TypeError("companion action definition has an unknown field");
    if (!ACTION_ID.test(definition.id) || this.definitions.has(definition.id)) throw new TypeError("companion action id is invalid or duplicated");
    if (typeof definition.label !== "string" || !definition.label.trim() || definition.label.length > 64) throw new TypeError("companion action label is invalid");
    if (!Array.isArray(definition.sources) || definition.sources.length === 0 || definition.sources.some((source) => !SOURCES.has(source))) {
      throw new TypeError("companion action sources are invalid");
    }
    if (typeof definition.handler !== "function") throw new TypeError("companion action handler is invalid");
    if (definition.validatePayload !== undefined && typeof definition.validatePayload !== "function") throw new TypeError("companion action payload validator is invalid");
    if (definition.enabled !== undefined && typeof definition.enabled !== "function" && typeof definition.enabled !== "boolean") {
      throw new TypeError("companion action feature flag is invalid");
    }
    if (definition.menu !== undefined) validateMenuMetadata(definition.menu);
    this.definitions.set(definition.id, Object.freeze({
      ...definition,
      label: definition.label.trim(),
      sources: Object.freeze([...new Set(definition.sources)]),
      enabled: definition.enabled ?? true,
    }));
  }

  isEnabled(id, context = {}) {
    const definition = this.definitions.get(id);
    if (!definition) return false;
    return typeof definition.enabled === "function" ? definition.enabled(context) === true : definition.enabled === true;
  }

  async execute(id, { source, payload = Object.freeze({}), context = Object.freeze({}) } = {}) {
    const definition = this.definitions.get(id);
    if (!definition) throw new Error("companion_action_unknown");
    if (!SOURCES.has(source) || !definition.sources.includes(source)) throw new Error("companion_action_source_rejected");
    if (!this.isEnabled(id, context)) return Object.freeze({ status: "disabled", action_id: id });
    const safePayload = definition.validatePayload ? definition.validatePayload(payload) : requireEmptyPayload(payload);
    return await definition.handler(safePayload, Object.freeze({ source, actionId: id, context }));
  }

  menuTemplate({ source = "pet", context = Object.freeze({}) } = {}) {
    if (!SOURCES.has(source)) throw new TypeError("companion menu source is invalid");
    const entries = [...this.definitions.values()]
      .filter((definition) => definition.menu && definition.sources.includes(source))
      .sort((left, right) => left.menu.order - right.menu.order);
    const template = [];
    let lastGroup = null;
    for (const definition of entries) {
      if (lastGroup !== null && definition.menu.group !== lastGroup) template.push(Object.freeze({ type: "separator" }));
      lastGroup = definition.menu.group;
      template.push(Object.freeze({
        id: definition.id,
        label: definition.label,
        enabled: this.isEnabled(definition.id, context),
        click: () => this.execute(definition.id, { source, context }),
      }));
    }
    return Object.freeze(template);
  }
}

function validateMenuMetadata(value) {
  if (!value || typeof value !== "object" || Array.isArray(value) || Object.keys(value).some((key) => !["group", "order"].includes(key))) {
    throw new TypeError("companion action menu metadata is invalid");
  }
  if (!Number.isSafeInteger(value.group) || value.group < 0 || value.group > 20) throw new TypeError("companion action menu group is invalid");
  if (!Number.isSafeInteger(value.order) || value.order < 0 || value.order > 10_000) throw new TypeError("companion action menu order is invalid");
}

function requireEmptyPayload(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload) || Object.keys(payload).length !== 0) {
    throw new Error("companion_action_payload_rejected");
  }
  return Object.freeze({});
}

function validatePanelPayload(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload) || Object.keys(payload).some((key) => key !== "panel")) {
    throw new Error("companion_panel_payload_rejected");
  }
  const panel = typeof payload.panel === "string" ? payload.panel : "";
  if (!COMPANION_PANEL_IDS.has(panel)) throw new Error("companion_panel_payload_rejected");
  return Object.freeze({ panel });
}

const COMPANION_PANEL_IDS = new Set([
  "chat", "character", "master_profile", "schedule", "focus", "inventory",
  "status", "launchers", "privacy", "voice_vision", "help_data",
]);

module.exports = {
  ACTION_ID,
  COMPANION_PANEL_IDS,
  CompanionActionRegistry,
  SOURCES,
  requireEmptyPayload,
  validatePanelPayload,
};
