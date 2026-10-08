const assert = require("node:assert/strict");
const test = require("node:test");

const { CompanionOverlayController, sanitiseOverlayEvent, sanitiseTextSubmission } = require("../src/companion/overlay-controller.cjs");

function event(overrides = {}) {
  return { event_id: "evt-1", kind: "reminder", visual_state: "attention", text: "该休息了", actions: [{ id: "ok", label: "知道了" }], requires_ack: true, ...overrides };
}

test("overlay events and text have closed schemas and hard size limits", () => {
  assert.equal(sanitiseOverlayEvent(event()).actions.length, 1);
  assert.throws(() => sanitiseOverlayEvent(event({ text: "x".repeat(401) })), /text_invalid/);
  assert.equal(sanitiseOverlayEvent(event({ actions: [
    { id: "acknowledge", label: "知道了" }, { id: "snooze_5m", label: "5 分钟后" }, { id: "complete", label: "完成" },
  ] })).actions.length, 3);
  assert.throws(() => sanitiseOverlayEvent(event({ actions: [
    { id: "one", label: "一" }, { id: "two", label: "二" }, { id: "three", label: "三" }, { id: "four", label: "四" },
  ] })), /actions_invalid/);
  assert.throws(() => sanitiseOverlayEvent(event({ url: "file:///secret" })), /unknown_field/);
  assert.equal(sanitiseTextSubmission({ request_id: "req-1", text: "x".repeat(4000) }).text.length, 4000);
  assert.throws(() => sanitiseTextSubmission({ request_id: "req-1", text: "x".repeat(4001) }), /submission_invalid/);
});

test("ambient, clipboard, and reminder events retain their internal action sets", () => {
  for (const kind of ["ambient_idle", "clipboard_changed", "reminder_due"]) {
    assert.equal(sanitiseOverlayEvent(event({ kind, actions: [{ id: "one", label: "一" }, { id: "two", label: "二" }] })).actions.length, 2);
  }
});

test("overlay actions are current, allowlisted, idempotent and retryable after failure", async () => {
  let failures = 1;
  const hidden = [];
  const emitted = [];
  const controller = new CompanionOverlayController({
    emitEvent: (value) => emitted.push(value), show() {}, hide: () => hidden.push(true), onSubmit: async (value) => value,
    onAction: async () => { if (failures-- > 0) throw new Error("transient"); return { status: "done" }; },
    onAcknowledge: async () => ({ status: "acknowledged" }),
  });
  controller.present(event());
  await assert.rejects(() => controller.perform({ event_id: "evt-1", action_id: "bad" }), /not_allowed/);
  await assert.rejects(() => controller.perform({ event_id: "evt-1", action_id: "ok" }), /transient/);
  assert.deepEqual(await controller.perform({ event_id: "evt-1", action_id: "ok" }), { status: "done" });
  assert.deepEqual(await controller.perform({ event_id: "evt-1", action_id: "ok" }), { status: "already_performed" });
  await controller.acknowledge({ event_id: "evt-1" });
  assert.equal(hidden.length, 1);
  assert.equal(emitted.at(-1), null);
  await assert.rejects(() => controller.perform({ event_id: "evt-1", action_id: "ok" }), /not_current/);
});

test("overlay projection is non-focusing by default and permits explicit user focus only", () => {
  const shown = [];
  const controller = new CompanionOverlayController({
    emitEvent() {}, show: (options) => shown.push(options), hide() {}, async onSubmit() {}, async onAction() {}, async onAcknowledge() {},
  });
  controller.present(event());
  assert.deepEqual(shown, [{ focus: false }]);
  controller.present(event(), { focus: true });
  assert.deepEqual(shown.at(-1), { focus: true });
  assert.equal("focus" in controller.current, false);
});

test("required reminders cannot be dismissed without an explicit action", () => {
  const hidden = [];
  const emitted = [];
  const controller = new CompanionOverlayController({
    emitEvent: (value) => emitted.push(value), show() {}, hide: () => hidden.push(true), async onSubmit() {}, async onAction() {}, async onAcknowledge() {},
  });
  controller.present(event());
  assert.deepEqual(controller.close(), { status: "requires_action" });
  assert.equal(controller.current.event_id, "evt-1");
  assert.equal(hidden.length, 0);
  assert.equal(emitted.length, 1);
  assert.deepEqual(controller.close({ force: true }), { status: "closed" });
  assert.equal(hidden.length, 1);
  assert.equal(emitted.at(-1), null);
});
