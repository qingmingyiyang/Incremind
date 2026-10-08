const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  CACHE_SCHEMA,
  collectInputs,
  createIdentity,
  identitiesEqual,
  inspectCache,
  readProof,
  refreshCache,
  shellSourceInputs,
  shellTargetInputs,
  writeProof,
} = require("../scripts/candidate-build-cache.cjs");

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-candidate-cache-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const source = path.join(root, "source");
  const target = path.join(root, "target");
  const proofPath = path.join(root, "cache", "proof.json");
  fs.mkdirSync(path.join(source, "nested"), { recursive: true });
  fs.mkdirSync(target, { recursive: true });
  fs.writeFileSync(path.join(source, "nested", "input.txt"), "source-a");
  fs.writeFileSync(path.join(target, "output.txt"), "target-a");
  return {
    proofPath,
    source,
    sourceInputs: [{ root: source, prefix: "source" }],
    target,
    targetInputs: [{ root: target, prefix: "target" }],
  };
}

test("creates deterministic identities without exposing absolute paths", async (t) => {
  const current = fixture(t);
  const first = await createIdentity(current.sourceInputs);
  const second = await createIdentity(current.sourceInputs);
  assert.equal(identitiesEqual(first, second), true);
  assert.equal(first.files, 1);
  assert.equal(first.total_size, 8);
  assert.equal(JSON.stringify(first).includes(current.source), false);
  assert.throws(
    () => collectInputs([{ root: current.source, prefix: "../escape" }]),
    /prefix is unsafe/,
  );
});

test("cache hits only when source and target bytes still match", async (t) => {
  const current = fixture(t);
  const policy = "test-policy";
  const missing = await inspectCache({
    proofPath: current.proofPath,
    policy,
    sourceInputs: current.sourceInputs,
    targetInputs: current.targetInputs,
  });
  assert.equal(missing.reason, "proof_missing");

  await refreshCache({
    proofPath: current.proofPath,
    policy,
    sourceInputs: current.sourceInputs,
    targetInputs: current.targetInputs,
  });
  const hit = await inspectCache({
    proofPath: current.proofPath,
    policy,
    sourceInputs: current.sourceInputs,
    targetInputs: current.targetInputs,
  });
  assert.equal(hit.hit, true);

  fs.writeFileSync(path.join(current.source, "nested", "input.txt"), "source-b");
  const sourceChanged = await inspectCache({
    proofPath: current.proofPath,
    policy,
    sourceInputs: current.sourceInputs,
    targetInputs: current.targetInputs,
  });
  assert.equal(sourceChanged.reason, "source_changed");

  fs.writeFileSync(path.join(current.source, "nested", "input.txt"), "source-a");
  fs.writeFileSync(path.join(current.target, "output.txt"), "target-b");
  const targetChanged = await inspectCache({
    proofPath: current.proofPath,
    policy,
    sourceInputs: current.sourceInputs,
    targetInputs: current.targetInputs,
  });
  assert.equal(targetChanged.reason, "target_changed");
});

test("malformed proofs fail closed and atomic proof writes remain bounded", async (t) => {
  const current = fixture(t);
  const [source, target] = await Promise.all([
    createIdentity(current.sourceInputs),
    createIdentity(current.targetInputs),
  ]);
  const proof = writeProof(current.proofPath, "policy", source, target, {
    now: () => new Date("2026-08-01T00:00:00.000Z"),
  });
  assert.equal(proof.schema_version, CACHE_SCHEMA);
  assert.equal(readProof(current.proofPath, "policy").hit, true);
  assert.equal(readProof(current.proofPath, "another-policy").reason, "proof_invalid");
  fs.writeFileSync(current.proofPath, "{broken");
  assert.equal(readProof(current.proofPath, "policy").reason, "proof_invalid");
  assert.equal(fs.existsSync(`${current.proofPath}.tmp`), false);
});

test("cache refresh and bypass never accept an existing proof", async (t) => {
  const current = fixture(t);
  await refreshCache({
    proofPath: current.proofPath,
    policy: "policy",
    sourceInputs: current.sourceInputs,
    targetInputs: current.targetInputs,
  });
  const refresh = await inspectCache({
    proofPath: current.proofPath,
    policy: "policy",
    sourceInputs: current.sourceInputs,
    targetInputs: current.targetInputs,
    refresh: true,
  });
  const bypass = await inspectCache({
    proofPath: current.proofPath,
    policy: "policy",
    sourceInputs: current.sourceInputs,
    targetInputs: current.targetInputs,
    useCache: false,
  });
  assert.equal(refresh.reason, "refresh_requested");
  assert.equal(bypass.reason, "cache_disabled");
});

test("candidate base-shell identity excludes replaceable sidecar and frontend payloads", () => {
  const sources = shellSourceInputs();
  assert.equal(sources.some((input) => input.prefix === "app/frontend-dist"), false);
  assert.equal(sources.some((input) => input.prefix === "app/src"), true);
  assert.equal(sources.some((input) => input.prefix === "electron-dist"), true);
  const [target] = shellTargetInputs();
  assert.equal(target.exclude("resources/sidecar", true), true);
  assert.equal(target.exclude("resources/sidecar/runtime/python.exe", false), true);
  assert.equal(target.exclude("resources/app/frontend-dist", true), true);
  assert.equal(target.exclude("resources/app/frontend-dist/assets/app.js", false), true);
  assert.equal(target.exclude("resources/app/src/main.cjs", false), false);
  assert.equal(target.exclude("Chriptmas OS.exe", false), false);
});

test("proof refresh rejects source changes during a build", async (t) => {
  const current = fixture(t);
  const expectedSource = await createIdentity(current.sourceInputs);
  fs.writeFileSync(path.join(current.source, "nested", "input.txt"), "source-b");
  await assert.rejects(
    refreshCache({
      proofPath: current.proofPath,
      policy: "policy",
      sourceInputs: current.sourceInputs,
      targetInputs: current.targetInputs,
      expectedSource,
    }),
    /source changed while the build was running/,
  );
  assert.equal(fs.existsSync(current.proofPath), false);
});
