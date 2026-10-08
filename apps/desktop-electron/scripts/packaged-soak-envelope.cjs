const fs = require("node:fs");

const METRICS = Object.freeze([
  "peak_rss_bytes",
  "peak_private_bytes",
  "peak_handles",
  "peak_threads",
  "peak_processes",
]);

function requiredInteger(value, label) {
  if (!Number.isSafeInteger(value) || value <= 0) throw new Error(`packaged_soak_envelope_${label}_invalid`);
  return value;
}

function evaluatePackagedSoakEnvelope({ envelopePath, candidatePath, metrics, expectedContract }) {
  const envelope = JSON.parse(fs.readFileSync(envelopePath, "utf8"));
  if (envelope?.schema_version !== "1.0.0" || typeof envelope.envelope_revision !== "string" || !envelope.envelope_revision) {
    throw new Error("packaged_soak_envelope_schema_invalid");
  }
  const stat = fs.statSync(candidatePath);
  if (requiredInteger(envelope.candidate?.size_bytes, "candidate_size") !== stat.size
    || envelope.candidate?.last_write_utc !== stat.mtime.toISOString()) {
    throw new Error("packaged_soak_envelope_candidate_mismatch");
  }
  for (const [name, expected] of Object.entries(expectedContract)) {
    if (requiredInteger(envelope.sample_contract?.[name], `contract_${name}`) !== expected) {
      throw new Error(`packaged_soak_envelope_contract_${name}_mismatch`);
    }
  }
  if (envelope.derivation?.method !== "observed_max_plus_full_observed_range"
    || !Array.isArray(envelope.derivation.validation_refs) || envelope.derivation.validation_refs.length !== 3) {
    throw new Error("packaged_soak_envelope_derivation_invalid");
  }
  const observed = {};
  for (const name of METRICS) {
    const limit = requiredInteger(envelope.limits?.[name], `limit_${name}`);
    const actual = requiredInteger(metrics?.[name], `metric_${name}`);
    observed[name] = { actual, limit };
    if (actual > limit) throw new Error(`packaged_soak_envelope_exceeded:${name}:${actual}:${limit}`);
  }
  return Object.freeze({
    status: "versioned_envelope_passed",
    revision: envelope.envelope_revision,
    baseline_count: envelope.sample_contract.baseline_count,
    observed,
  });
}

module.exports = { evaluatePackagedSoakEnvelope };
