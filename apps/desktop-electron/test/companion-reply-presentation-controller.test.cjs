const assert = require("node:assert/strict");
const test = require("node:test");

const { CompanionReplyPresentationController } = require("../src/companion/reply-presentation-controller.cjs");

function harness({ quiet = false, sleeping = false } = {}) {
  const overlays = [];
  const appearance = [];
  const spoken = [];
  const timers = new Map();
  const cleared = [];
  let nextTimer = 1;
  const controller = new CompanionReplyPresentationController({
    overlayController: { present: (...args) => overlays.push(args) },
    appearanceArbiter: {
      set: (...args) => appearance.push(["set", ...args]),
      clear: (...args) => appearance.push(["clear", ...args]),
    },
    isQuiet: () => quiet,
    isSleeping: () => sleeping,
    speak: async (text) => { spoken.push(text); },
    setTimeoutFn(callback, milliseconds) {
      const id = nextTimer++;
      timers.set(id, { callback, milliseconds });
      return id;
    },
    clearTimeoutFn(id) { cleared.push(id); timers.delete(id); },
    now: () => 46655,
  });
  return { appearance, cleared, controller, overlays, spoken, timers };
}

test("rejects empty oversized NUL and non-string replies before presentation", () => {
  const value = harness();
  for (const input of [null, "", "   ", `x${"a".repeat(4000)}`, "bad\0text"]) {
    assert.throws(() => value.controller.present(input), /companion_reply_invalid/);
  }
  assert.deepEqual(value.overlays, []);
  assert.equal(value.timers.size, 0);
});

test("projects bounded text talk appearance duration and optional speech", async () => {
  const value = harness();
  const text = `  ${"文".repeat(450)}  `;
  assert.deepEqual(value.controller.present(text), { status: "presented" });
  assert.equal(value.overlays[0][0].event_id, "chat-reply:zzz");
  assert.equal(value.overlays[0][0].text.length, 400);
  assert.deepEqual(value.overlays[0][1], { focus: false });
  assert.deepEqual(value.appearance[0], [
    "set", "interactive", { state: "speaking", mood: "calm", animation_key: "talk" }, { ttlMs: 8000 },
  ]);
  assert.equal([...value.timers.values()][0].milliseconds, 8000);
  await Promise.resolve();
  assert.deepEqual(value.spoken, ["文".repeat(400)]);
});

test("quiet and sleep suppress talk or speech without hiding the reply", async () => {
  const quiet = harness({ quiet: true });
  quiet.controller.present("安静展示");
  await Promise.resolve();
  assert.equal(quiet.overlays.length, 1);
  assert.deepEqual(quiet.appearance, []);
  assert.deepEqual(quiet.spoken, []);

  const sleeping = harness({ sleeping: true });
  sleeping.controller.present("睡眠展示");
  await Promise.resolve();
  assert.equal(sleeping.overlays.length, 1);
  assert.equal(sleeping.appearance[0][0], "set");
  assert.deepEqual(sleeping.spoken, []);

  const silent = harness();
  silent.controller.present("不要朗读", { speak: false });
  await Promise.resolve();
  assert.deepEqual(silent.spoken, []);
});

test("new replies replace the timer and cancel clears the interactive owner", () => {
  const value = harness();
  value.controller.present("第一条");
  const first = [...value.timers.keys()][0];
  value.controller.present("第二条");
  assert.deepEqual(value.cleared, [first]);
  assert.equal(value.timers.size, 1);
  const [second, timer] = [...value.timers.entries()][0];
  timer.callback();
  assert.equal(value.controller.timer, null);
  assert.deepEqual(value.appearance.at(-1), ["clear", "interactive"]);
  value.controller.cancel();
  assert.equal(value.cleared.includes(second), false);
  assert.deepEqual(value.appearance.at(-1), ["clear", "interactive"]);
});
