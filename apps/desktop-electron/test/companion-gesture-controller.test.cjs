const assert = require("node:assert/strict");
const test = require("node:test");

const {
  CompanionGestureController,
  DOUBLE_CLICK_WINDOW_MS,
  DRAG_THRESHOLD,
  PETTING_COOLDOWN_MS,
  isHead,
} = require("../src/companion/gesture-controller.cjs");

function harness() {
  let now = 1_000;
  let nextTimer = 1;
  const timers = new Map();
  const intents = [];
  const controller = new CompanionGestureController({
    onIntent: (intent) => intents.push(intent),
    now: () => now,
    setTimeoutFn: (callback, delay) => {
      const id = nextTimer++;
      timers.set(id, { callback, due: now + delay });
      return id;
    },
    clearTimeoutFn: (id) => timers.delete(id),
  });
  const advance = (milliseconds) => {
    now += milliseconds;
    for (const [id, timer] of [...timers]) {
      if (timer.due <= now) {
        timers.delete(id);
        timer.callback();
      }
    }
  };
  return { controller, intents, advance, timers };
}

function clickSequence(controller, suffix, { x = 0.5, y = 0.2, count = 1, moves = [] } = {}) {
  const gestureId = `gesture-test-${suffix}`;
  controller.begin({ gesture_id: gestureId, pointer_kind: "mouse", x, y });
  for (const [dx, dy] of moves) controller.move({ gesture_id: gestureId, dx, dy });
  controller.end({ gesture_id: gestureId, cancelled: false });
  return controller.click({ gesture_id: gestureId, x, y, click_count: count });
}

test("single head click waits out the double-click window before petting", () => {
  const { controller, intents, advance } = harness();
  assert.equal(clickSequence(controller, "one").status, "petting_pending");
  advance(DOUBLE_CLICK_WINDOW_MS - 1);
  assert.deepEqual(intents, []);
  advance(1);
  assert.deepEqual(intents, [{ intent: "petting", source: "head_single_click" }]);
});

test("double click cancels pending petting and opens main exactly once", () => {
  const { controller, intents, advance } = harness();
  clickSequence(controller, "first");
  assert.equal(clickSequence(controller, "second", { count: 2 }).status, "open_main");
  advance(DOUBLE_CLICK_WINDOW_MS);
  assert.deepEqual(intents, [{ intent: "open_main", source: "double_click" }]);
});

test("body single click is inert while body double click opens main", () => {
  const { controller, intents, advance } = harness();
  assert.equal(clickSequence(controller, "body1", { y: 0.8 }).status, "body_single_ignored");
  advance(DOUBLE_CLICK_WINDOW_MS);
  assert.deepEqual(intents, []);
  clickSequence(controller, "body2", { y: 0.8, count: 2 });
  assert.deepEqual(intents, [{ intent: "open_main", source: "double_click" }]);
});

test("touchpad jitter remains clickable and movement above eight pixels cancels click", () => {
  const { controller, intents, advance } = harness();
  assert.equal(clickSequence(controller, "jitter", { moves: [[3, 4], [1, 1]] }).status, "petting_pending");
  advance(DOUBLE_CLICK_WINDOW_MS);
  assert.equal(intents.at(-1).intent, "petting");
  assert.equal(clickSequence(controller, "drag", { moves: [[DRAG_THRESHOLD, 1]] }).status, "cancelled_by_drag");
});

test("petting cooldown emits only a bounded cooldown intent", () => {
  const { controller, intents, advance } = harness();
  clickSequence(controller, "cool1");
  advance(DOUBLE_CLICK_WINDOW_MS);
  advance(10_000);
  clickSequence(controller, "cool2");
  advance(DOUBLE_CLICK_WINDOW_MS);
  assert.deepEqual(intents.at(-1), { intent: "petting_cooldown", retry_after_ms: PETTING_COOLDOWN_MS - 10_000 - DOUBLE_CLICK_WINDOW_MS });
  assert.equal("affinity" in intents.at(-1), false);
  assert.equal("wallet" in intents.at(-1), false);
});

test("cancel, stale IDs, cancelled pointers, and malformed facts fail closed", () => {
  const { controller, intents, advance, timers } = harness();
  clickSequence(controller, "cancel");
  controller.cancelActive();
  assert.equal(timers.size, 0);
  advance(DOUBLE_CLICK_WINDOW_MS);
  assert.deepEqual(intents, []);
  assert.throws(() => controller.move({ gesture_id: "gesture-test-stale", dx: 1, dy: 1 }), /not_current/);
  controller.begin({ gesture_id: "gesture-test-ended", pointer_kind: "touch", x: 0.5, y: 0.2 });
  controller.end({ gesture_id: "gesture-test-ended", cancelled: true });
  assert.equal(controller.click({ gesture_id: "gesture-test-ended", x: 0.5, y: 0.2, click_count: 1 }).status, "cancelled_by_drag");
  assert.throws(() => controller.begin({ gesture_id: "bad", pointer_kind: "mouse", x: 0.5, y: 0.2 }), /gesture_id_invalid/);
  assert.throws(() => controller.begin({ gesture_id: "gesture-test-extra", pointer_kind: "mouse", x: 0.5, y: 0.2, title: "secret" }), /payload_invalid/);
  assert.throws(() => controller.move({ gesture_id: "gesture-test-extra", dx: 81, dy: 0 }), /delta_invalid/);
});

test("head region is normalized and bounded", () => {
  assert.equal(isHead(0.2, 0.02), true);
  assert.equal(isHead(0.8, 0.4), true);
  assert.equal(isHead(0.5, 0.41), false);
});
