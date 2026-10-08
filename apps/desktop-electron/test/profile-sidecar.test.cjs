const test = require("node:test");
const assert = require("node:assert/strict");

const { median } = require("../scripts/profile-sidecar.cjs");

test("reports a stable median without mutating samples", () => {
  const odd = [9, 1, 5];
  const even = [8, 2, 6, 4];
  assert.equal(median(odd), 5);
  assert.equal(median(even), 5);
  assert.deepEqual(odd, [9, 1, 5]);
  assert.throws(() => median([]), /requires samples/);
});
