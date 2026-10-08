const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { evaluatePackagedSoakEnvelope } = require("../scripts/packaged-soak-envelope.cjs");

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "packaged-soak-envelope-"));
  const candidatePath = path.join(root, "candidate.exe");
  fs.writeFileSync(candidatePath, "candidate", "utf8");
  const stat = fs.statSync(candidatePath);
  const envelopePath = path.join(root, "envelope.json");
  const envelope = {
    schema_version: "1.0.0", envelope_revision: "test-r1",
    candidate: { size_bytes: stat.size, last_write_utc: stat.mtime.toISOString() },
    sample_contract: { duration_seconds: 3600, sample_interval_seconds: 10, minimum_samples: 360, baseline_count: 3 },
    derivation: { method: "observed_max_plus_full_observed_range", validation_refs: ["one", "two", "three"] },
    limits: { peak_rss_bytes: 130, peak_private_bytes: 100, peak_handles: 40, peak_threads: 20, peak_processes: 8 },
  };
  fs.writeFileSync(envelopePath, JSON.stringify(envelope), "utf8");
  return { root, candidatePath, envelopePath, envelope };
}

const contract = { duration_seconds: 3600, sample_interval_seconds: 10, minimum_samples: 360, baseline_count: 3 };
const metrics = { peak_rss_bytes: 120, peak_private_bytes: 90, peak_handles: 39, peak_threads: 19, peak_processes: 8 };

test("accepts matching candidate, contract, derivation, and bounded metrics", (t) => {
  const value = fixture();
  t.after(() => fs.rmSync(value.root, { recursive: true, force: true }));
  const result = evaluatePackagedSoakEnvelope({ ...value, metrics, expectedContract: contract });
  assert.equal(result.status, "versioned_envelope_passed");
  assert.equal(result.baseline_count, 3);
});

test("fails closed on candidate drift and metric overflow", (t) => {
  const value = fixture();
  t.after(() => fs.rmSync(value.root, { recursive: true, force: true }));
  fs.appendFileSync(value.candidatePath, "drift", "utf8");
  assert.throws(() => evaluatePackagedSoakEnvelope({ ...value, metrics, expectedContract: contract }), /candidate_mismatch/);
  fs.writeFileSync(value.candidatePath, "candidate", "utf8");
  const stat = fs.statSync(value.candidatePath);
  value.envelope.candidate = { size_bytes: stat.size, last_write_utc: stat.mtime.toISOString() };
  fs.writeFileSync(value.envelopePath, JSON.stringify(value.envelope), "utf8");
  assert.throws(() => evaluatePackagedSoakEnvelope({ ...value, metrics: { ...metrics, peak_handles: 41 }, expectedContract: contract }), /exceeded:peak_handles:41:40/);
});

test("fails closed on sample contract or derivation drift", (t) => {
  const value = fixture();
  t.after(() => fs.rmSync(value.root, { recursive: true, force: true }));
  assert.throws(() => evaluatePackagedSoakEnvelope({ ...value, metrics, expectedContract: { ...contract, minimum_samples: 361 } }), /minimum_samples_mismatch/);
  value.envelope.derivation.validation_refs = ["one", "two"];
  fs.writeFileSync(value.envelopePath, JSON.stringify(value.envelope), "utf8");
  assert.throws(() => evaluatePackagedSoakEnvelope({ ...value, metrics, expectedContract: contract }), /derivation_invalid/);
});
