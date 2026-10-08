const assert = require("node:assert/strict");
const test = require("node:test");

const { CompanionMemoryAttentionController } = require("../src/companion/memory-attention-controller.cjs");

test("presents one bounded review action per nonzero pending epoch", () => {
  const events = [];
  const controller = new CompanionMemoryAttentionController({ present: (event) => events.push(event), isQuiet: () => false });
  assert.equal(controller.update({ pending_memory_candidate_count: 2 }).status, "presented");
  assert.equal(controller.update({ pending_memory_candidate_count: 3 }).status, "already_notified");
  assert.equal(events.length, 1);
  assert.deepEqual(events[0], {
    event_id: "memory-review-2", kind: "memory_review_ready", visual_state: "attention",
    text: "有 2 条记忆候选等你确认。确认前，它们不会进入长期记忆。",
    actions: [{ id: "memory.review", label: "去审阅" }], requires_ack: false,
  });
  assert.equal(controller.hasPending(), true);
  controller.update({ pending_memory_candidate_count: 0 });
  assert.equal(controller.hasPending(), false);
  assert.equal(controller.update({ pending_memory_candidate_count: 1 }).status, "presented");
  assert.equal(events.length, 2);
});

test("defers without consuming the epoch while quiet and rejects invalid counts", () => {
  const events = [];
  let quiet = true;
  const controller = new CompanionMemoryAttentionController({ present: (event) => events.push(event), isQuiet: () => quiet });
  assert.equal(controller.update({ pending_memory_candidate_count: 1 }).status, "suppressed");
  assert.equal(events.length, 0);
  quiet = false;
  assert.equal(controller.update({ pending_memory_candidate_count: 1 }).status, "presented");
  assert.equal(controller.update({ pending_memory_candidate_count: Number.POSITIVE_INFINITY }).status, "idle");
  assert.equal(controller.hasPending(), false);
});
