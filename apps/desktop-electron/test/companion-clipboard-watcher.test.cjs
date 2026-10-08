const assert = require("node:assert/strict");
const test = require("node:test");

const { CompanionClipboardWatcher, analyseText, classifySensitive, lengthBucket } = require("../src/companion/clipboard-watcher.cjs");

function harness({ quiet = false, initial = "" } = {}) {
  let value = initial;
  const events = [];
  const intervals = new Map();
  const timeouts = new Map();
  let nextTimer = 1;
  const clipboard = {
    readText: () => value,
    clear: () => { value = ""; },
    writeText: (next) => { value = next; },
  };
  const watcher = new CompanionClipboardWatcher({
    clipboard, onEvent: (event) => events.push(event), isQuiet: () => quiet, now: () => 1000,
    setIntervalFn: (fn, delay) => { const id = nextTimer++; intervals.set(id, { fn, delay }); return id; },
    clearIntervalFn: (id) => intervals.delete(id),
    setTimeoutFn: (fn, delay) => { const id = nextTimer++; timeouts.set(id, { fn, delay }); return id; },
    clearTimeoutFn: (id) => timeouts.delete(id),
  });
  return { watcher, clipboard, events, intervals, timeouts, setValue: (next) => { value = next; }, getValue: () => value, setQuiet: (next) => { quiet = next; } };
}

test("watcher is opt-in, primes without announcing, and polls once per second", () => {
  const h = harness({ initial: "already there" });
  assert.deepEqual(h.watcher.status(), { enabled: false, state: "disabled", length_bucket: "empty", sensitive: false, undo_available: false });
  assert.deepEqual(h.watcher.setEnabled(true), { enabled: true, state: "ready", length_bucket: "short", sensitive: false, undo_available: false });
  assert.equal([...h.intervals.values()][0].delay, 1000);
  h.watcher.poll();
  assert.equal(h.events.length, 0);
  h.setValue("TODO: write tests");
  h.watcher.poll();
  assert.equal(h.events.length, 1);
  assert.equal("focus" in h.events[0], false);
  assert.equal(JSON.stringify(h.events[0]).includes("TODO: write tests"), false);
});

test("inspect is local-only, eat clears, undo restores once, and self-write does not loop", () => {
  const h = harness();
  h.watcher.setEnabled(true);
  h.setValue("TODO: fix this error in function main");
  h.watcher.poll();
  const inspected = h.watcher.inspect(h.events[0].event_id);
  assert.match(inspected.text, /代码|报错|待办/);
  assert.doesNotMatch(inspected.text, /fix this error/);
  const eaten = h.watcher.eat(inspected.event_id);
  assert.equal(h.getValue(), "");
  assert.equal(h.watcher.status().undo_available, true);
  assert.equal([...h.timeouts.values()][0].delay, 10_000);
  const restored = h.watcher.undo(eaten.event_id);
  assert.equal(restored.kind, "clipboard_restored");
  assert.equal(h.getValue(), "TODO: fix this error in function main");
  h.watcher.poll();
  assert.equal(h.events.length, 1);
  assert.throws(() => h.watcher.undo(eaten.event_id), /not_current/);
});

test("undo expires and disable or stop drops all volatile text", () => {
  const h = harness();
  h.watcher.setEnabled(true);
  h.setValue("temporary");
  h.watcher.poll();
  const eaten = h.watcher.eat(h.events[0].event_id);
  [...h.timeouts.values()][0].fn();
  assert.equal(h.watcher.status().undo_available, false);
  assert.throws(() => h.watcher.undo(eaten.event_id), /expired/);
  h.setValue("another secret-ish value");
  h.watcher.poll();
  h.watcher.setEnabled(false);
  assert.deepEqual(h.watcher.status(), { enabled: false, state: "disabled", length_bucket: "empty", sensitive: false, undo_available: false });
  assert.equal(h.intervals.size, 0);
  h.watcher.stop();
  assert.equal(h.timeouts.size, 0);
});

test("sensitive, private-key, high-entropy and oversized values never appear in projections", () => {
  const samples = [
    "password=hunter2",
    "-----BEGIN PRIVATE KEY-----\nABCDEF",
    "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdef",
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "x".repeat(4001),
  ];
  for (const sample of samples) {
    const h = harness();
    h.watcher.setEnabled(true);
    h.setValue(sample);
    h.watcher.poll();
    assert.equal(h.watcher.status().sensitive, true);
    assert.equal(JSON.stringify(h.events[0]).includes(sample), false);
    const inspected = h.watcher.inspect(h.events[0].event_id);
    assert.match(inspected.text, /不会显示、保存或发送/);
    assert.equal(JSON.stringify(inspected).includes(sample), false);
  }
});

test("quiet mode, empty/image clipboard and rapid changes update state without stale bubbles", () => {
  const h = harness({ quiet: true });
  h.watcher.setEnabled(true);
  h.setValue("first"); h.watcher.poll();
  h.setValue("second"); h.watcher.poll();
  assert.equal(h.events.length, 0);
  assert.equal(h.watcher.status().length_bucket, "short");
  h.setQuiet(false);
  h.setValue(""); h.watcher.poll();
  assert.equal(h.events.length, 0);
  h.setValue("third"); h.watcher.poll();
  assert.equal(h.events.length, 1);
  assert.throws(() => h.watcher.inspect("clipboard:old:1:1"), /not_current/);
});

test("classification exposes only coarse analysis", () => {
  assert.equal(lengthBucket(0), "empty");
  assert.equal(lengthBucket(81), "medium");
  assert.equal(lengthBucket(4001), "oversized");
  assert.equal(classifySensitive("ordinary sentence").sensitive, false);
  assert.deepEqual(analyseText("https://example.com\nTODO"), {
    sensitive: false, marker: false, oversized: false, high_entropy: false,
    length_bucket: "short", line_count: 2, keywords: ["link", "todo"],
  });
});
