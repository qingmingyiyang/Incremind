const assert = require("node:assert/strict");
const test = require("node:test");
const { CompanionEventConsumer, CompanionReminderPresenter } = require("../src/companion/reminder-runtime-controller.cjs");

test("event consumer has one bounded transport poll and prevents overlap", async () => {
  const intervals = [];
  let resolveRead;
  let reads = 0;
  const consumer = new CompanionEventConsumer({
    readEvent: () => { reads += 1; return new Promise((resolve) => { resolveRead = resolve; }); },
    onEvent() {}, setIntervalFn: (fn, ms) => { intervals.push({ fn, ms }); return 7; }, clearIntervalFn() {},
  });
  assert.equal(consumer.start(), true);
  assert.equal(consumer.start(), false);
  await Promise.resolve();
  assert.equal(reads, 1);
  const overlapping = consumer.tick();
  assert.equal(await overlapping, null);
  resolveRead(null);
  await Promise.resolve();
  assert.equal(intervals[0].ms, 1000);
  assert.equal(consumer.stop(), true);
});

test("critical reminder restores original topmost state and only beeps finitely", () => {
  const topmost = [];
  const shown = [];
  const timers = [];
  let beeps = 0;
  const window = { isDestroyed: () => false, isAlwaysOnTop: () => false, setAlwaysOnTop: (value) => topmost.push(value), show() {}, focus() {} };
  const overlay = { present: (event, options) => shown.push({ event, options }), close: (options) => shown.push({ close: options }) };
  const presenter = new CompanionReminderPresenter({
    windowProvider: () => window, overlayController: overlay, beep: () => { beeps += 1; },
    setTimeoutFn: (fn, delay) => { timers.push({ fn, delay }); return delay; }, clearTimeoutFn() {},
  });
  presenter.present({ event_id: "occ_1", kind: "reminder_due", visual_state: "attention", text: "时间到了", actions: ["acknowledge", "snooze_5m", "complete"], requires_ack: true });
  assert.deepEqual(topmost, [true]);
  assert.equal(beeps, 1);
  assert.deepEqual(timers.map((item) => item.delay), [2000, 4000]);
  timers.forEach((item) => item.fn());
  assert.equal(beeps, 3);
  assert.equal(presenter.settle("occ_1"), true);
  assert.deepEqual(topmost, [true, false]);
  assert.deepEqual(shown[0].event.actions.map((item) => item.id), ["acknowledge", "snooze_5m", "complete"]);
});

test("destroyed or missing main window does not prevent reminder projection", () => {
  const shown = [];
  const presenter = new CompanionReminderPresenter({
    windowProvider: () => null, overlayController: { present: (event) => shown.push(event), close() {} }, beep() {},
    setTimeoutFn: () => 1, clearTimeoutFn() {},
  });
  presenter.present({ event_id: "occ_2", kind: "reminder_due", visual_state: "attention", text: "事项", actions: ["acknowledge"], requires_ack: true });
  presenter.shutdown();
  assert.equal(shown.length, 1);
});
