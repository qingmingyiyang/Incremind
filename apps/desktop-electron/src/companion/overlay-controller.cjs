const EVENT_ID = /^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$/;
const VISUAL_STATES = new Set(["idle", "happy", "attention", "working", "speaking", "offline", "sleeping", "warning"]);

class CompanionOverlayController {
  constructor({ emitEvent, show, hide, onSubmit, onAction, onAcknowledge }) {
    for (const [name, value] of Object.entries({ emitEvent, show, hide, onSubmit, onAction, onAcknowledge })) {
      if (typeof value !== "function") throw new TypeError(`overlay ${name} handler is required`);
    }
    this.emitEvent = emitEvent;
    this.show = show;
    this.hide = hide;
    this.onSubmit = onSubmit;
    this.onAction = onAction;
    this.onAcknowledge = onAcknowledge;
    this.current = null;
    this.consumedActions = new Set();
  }

  present(payload, options = {}) {
    const event = sanitiseOverlayEvent(payload);
    this.current = event;
    this.consumedActions.clear();
    this.emitEvent(event);
    this.show(Object.freeze({ focus: options.focus === true }));
    return event;
  }

  async submit(payload) {
    const request = sanitiseTextSubmission(payload);
    return await this.onSubmit(request);
  }

  async perform(payload) {
    const action = sanitiseActionRequest(payload);
    const current = this.current;
    if (!current || current.event_id !== action.event_id) throw new Error("overlay_event_not_current");
    if (!current.actions.some((candidate) => candidate.id === action.action_id)) throw new Error("overlay_action_not_allowed");
    const identity = `${action.event_id}:${action.action_id}`;
    if (this.consumedActions.has(identity)) return Object.freeze({ status: "already_performed" });
    this.consumedActions.add(identity);
    try {
      return await this.onAction(action);
    } catch (error) {
      this.consumedActions.delete(identity);
      throw error;
    }
  }

  async acknowledge(payload) {
    const eventId = requireEventId(payload?.event_id);
    if (!this.current || this.current.event_id !== eventId) throw new Error("overlay_event_not_current");
    const result = await this.onAcknowledge(Object.freeze({ event_id: eventId }));
    this.close({ force: true });
    return result;
  }

  close({ force = false } = {}) {
    if (this.current?.requires_ack && !force) return Object.freeze({ status: "requires_action" });
    this.current = null;
    this.consumedActions.clear();
    this.emitEvent(null);
    this.hide();
    return Object.freeze({ status: "closed" });
  }
}

function sanitiseOverlayEvent(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw new Error("overlay_event_invalid");
  const allowed = new Set(["event_id", "kind", "visual_state", "text", "actions", "requires_ack"]);
  if (Object.keys(payload).some((key) => !allowed.has(key))) throw new Error("overlay_event_unknown_field");
  const eventId = requireEventId(payload.event_id);
  const kind = safeToken(payload.kind, 64, "overlay_event_kind_invalid");
  const text = typeof payload.text === "string" ? payload.text.trim() : "";
  if (text.length > 400 || text.includes("\0")) throw new Error("overlay_event_text_invalid");
  if (!VISUAL_STATES.has(payload.visual_state)) throw new Error("overlay_event_visual_invalid");
  if (!Array.isArray(payload.actions) || payload.actions.length > 3) throw new Error("overlay_event_actions_invalid");
  const actionIds = new Set();
  const actions = payload.actions.map((action) => {
    if (!action || typeof action !== "object" || Array.isArray(action) || Object.keys(action).some((key) => !["id", "label"].includes(key))) {
      throw new Error("overlay_event_action_invalid");
    }
    const id = safeToken(action.id, 48, "overlay_event_action_invalid");
    const label = typeof action.label === "string" ? action.label.trim() : "";
    if (!label || label.length > 48 || actionIds.has(id)) throw new Error("overlay_event_action_invalid");
    actionIds.add(id);
    return Object.freeze({ id, label });
  });
  const requiresAck = payload.requires_ack === true;
  return Object.freeze({ event_id: eventId, kind, visual_state: payload.visual_state, text, actions: Object.freeze(actions), requires_ack: requiresAck });
}

function sanitiseTextSubmission(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload) || Object.keys(payload).some((key) => !["request_id", "text"].includes(key))) {
    throw new Error("overlay_text_submission_invalid");
  }
  const requestId = requireEventId(payload.request_id);
  const text = typeof payload.text === "string" ? payload.text.trim() : "";
  if (!text || text.length > 4000 || text.includes("\0")) throw new Error("overlay_text_submission_invalid");
  return Object.freeze({ request_id: requestId, text });
}

function sanitiseActionRequest(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload) || Object.keys(payload).some((key) => !["event_id", "action_id"].includes(key))) {
    throw new Error("overlay_action_request_invalid");
  }
  return Object.freeze({ event_id: requireEventId(payload.event_id), action_id: safeToken(payload.action_id, 48, "overlay_action_request_invalid") });
}

function requireEventId(value) {
  if (typeof value !== "string" || !EVENT_ID.test(value)) throw new Error("overlay_event_id_invalid");
  return value;
}

function safeToken(value, limit, code) {
  if (typeof value !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._:-]*$/.test(value) || value.length > limit) throw new Error(code);
  return value;
}

module.exports = {
  CompanionOverlayController,
  sanitiseActionRequest,
  sanitiseOverlayEvent,
  sanitiseTextSubmission,
};
