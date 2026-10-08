const GESTURE_ID = /^gesture-[a-z0-9]{1,32}-[a-z0-9]{1,16}$/;
const POINTER_KINDS = new Set(["mouse", "pen", "touch"]);
const DRAG_THRESHOLD = 8;
const PETTING_COOLDOWN_MS = 60_000;
const DOUBLE_CLICK_WINDOW_MS = 260;
const HEAD_REGION = Object.freeze({ minX: 0.2, maxX: 0.8, minY: 0.02, maxY: 0.4 });

class CompanionGestureController {
  constructor({ onIntent, now = () => Date.now(), setTimeoutFn = setTimeout, clearTimeoutFn = clearTimeout }) {
    if (typeof onIntent !== "function") throw new TypeError("gesture intent handler is required");
    this.onIntent = onIntent;
    this.now = now;
    this.setTimeoutFn = setTimeoutFn;
    this.clearTimeoutFn = clearTimeoutFn;
    this.active = null;
    this.lastEnded = null;
    this.pendingPet = null;
    this.lastPettingAt = Number.NEGATIVE_INFINITY;
  }

  begin(payload) {
    const gesture = validateBegin(payload);
    this.active = { ...gesture, distance: 0, moved: false };
    return Object.freeze({ status: "tracking", gesture_id: gesture.gesture_id });
  }

  move(payload) {
    const delta = validateMove(payload);
    const active = this._requireActive(delta.gesture_id);
    active.distance += Math.hypot(delta.dx, delta.dy);
    if (active.distance > DRAG_THRESHOLD) active.moved = true;
    return Object.freeze({ status: active.moved ? "dragging" : "tracking", distance_bucket: distanceBucket(active.distance) });
  }

  end(payload) {
    const ending = validateEnd(payload);
    const active = this._requireActive(ending.gesture_id);
    this.active = null;
    this.lastEnded = {
      gesture_id: active.gesture_id,
      moved: active.moved || ending.cancelled,
      ended_at: this.now(),
    };
    return Object.freeze({ status: this.lastEnded.moved ? "dragged" : "click_candidate" });
  }

  click(payload) {
    const click = validateClick(payload);
    const ended = this.lastEnded;
    if (!ended || ended.gesture_id !== click.gesture_id || this.now() - ended.ended_at > 1000) {
      throw new Error("gesture_click_not_current");
    }
    if (ended.moved) return Object.freeze({ status: "cancelled_by_drag" });
    this.lastEnded = null;
    if (click.click_count >= 2) {
      this._cancelPendingPet();
      this._emit(Object.freeze({ intent: "open_main", source: "double_click" }));
      return Object.freeze({ status: "open_main" });
    }
    if (!isHead(click.x, click.y)) return Object.freeze({ status: "body_single_ignored" });
    this._cancelPendingPet();
    const token = click.gesture_id;
    const timer = this.setTimeoutFn(() => {
      if (!this.pendingPet || this.pendingPet.token !== token) return;
      this.pendingPet = null;
      const now = this.now();
      if (now - this.lastPettingAt < PETTING_COOLDOWN_MS) {
        this._emit(Object.freeze({ intent: "petting_cooldown", retry_after_ms: PETTING_COOLDOWN_MS - (now - this.lastPettingAt) }));
        return;
      }
      this.lastPettingAt = now;
      this._emit(Object.freeze({ intent: "petting", source: "head_single_click" }));
    }, DOUBLE_CLICK_WINDOW_MS);
    this.pendingPet = { token, timer };
    return Object.freeze({ status: "petting_pending" });
  }

  cancelActive() {
    this.active = null;
    this.lastEnded = null;
    this._cancelPendingPet();
  }

  dispose() {
    this.cancelActive();
  }

  _requireActive(gestureId) {
    if (!this.active || this.active.gesture_id !== gestureId) throw new Error("gesture_not_current");
    return this.active;
  }

  _cancelPendingPet() {
    if (this.pendingPet) this.clearTimeoutFn(this.pendingPet.timer);
    this.pendingPet = null;
  }

  _emit(intent) {
    Promise.resolve(this.onIntent(intent)).catch(() => {});
  }
}

function validateBegin(payload) {
  requireShape(payload, ["gesture_id", "pointer_kind", "x", "y"]);
  return Object.freeze({
    gesture_id: requireGestureId(payload.gesture_id),
    pointer_kind: requirePointerKind(payload.pointer_kind),
    x: requireRatio(payload.x),
    y: requireRatio(payload.y),
  });
}

function validateMove(payload) {
  requireShape(payload, ["gesture_id", "dx", "dy"]);
  return Object.freeze({ gesture_id: requireGestureId(payload.gesture_id), dx: requireDelta(payload.dx), dy: requireDelta(payload.dy) });
}

function validateEnd(payload) {
  requireShape(payload, ["gesture_id", "cancelled"]);
  if (typeof payload.cancelled !== "boolean") throw new Error("gesture_end_invalid");
  return Object.freeze({ gesture_id: requireGestureId(payload.gesture_id), cancelled: payload.cancelled });
}

function validateClick(payload) {
  requireShape(payload, ["gesture_id", "x", "y", "click_count"]);
  if (!Number.isSafeInteger(payload.click_count) || payload.click_count < 1 || payload.click_count > 2) throw new Error("gesture_click_invalid");
  return Object.freeze({ gesture_id: requireGestureId(payload.gesture_id), x: requireRatio(payload.x), y: requireRatio(payload.y), click_count: payload.click_count });
}

function requireShape(payload, keys) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload) || Object.keys(payload).length !== keys.length || keys.some((key) => !(key in payload))) {
    throw new Error("gesture_payload_invalid");
  }
}

function requireGestureId(value) {
  if (typeof value !== "string" || !GESTURE_ID.test(value)) throw new Error("gesture_id_invalid");
  return value;
}

function requirePointerKind(value) {
  if (!POINTER_KINDS.has(value)) throw new Error("gesture_pointer_invalid");
  return value;
}

function requireRatio(value) {
  if (!Number.isFinite(value) || value < 0 || value > 1) throw new Error("gesture_coordinate_invalid");
  return Math.round(value * 10_000) / 10_000;
}

function requireDelta(value) {
  if (!Number.isFinite(value) || Math.abs(value) > 80) throw new Error("gesture_delta_invalid");
  return Math.round(value * 100) / 100;
}

function isHead(x, y) {
  return x >= HEAD_REGION.minX && x <= HEAD_REGION.maxX && y >= HEAD_REGION.minY && y <= HEAD_REGION.maxY;
}

function distanceBucket(distance) {
  if (distance <= 2) return "still";
  if (distance <= DRAG_THRESHOLD) return "jitter";
  return "drag";
}

module.exports = {
  CompanionGestureController,
  DOUBLE_CLICK_WINDOW_MS,
  DRAG_THRESHOLD,
  GESTURE_ID,
  HEAD_REGION,
  PETTING_COOLDOWN_MS,
  POINTER_KINDS,
  distanceBucket,
  isHead,
  validateBegin,
  validateClick,
  validateEnd,
  validateMove,
};
