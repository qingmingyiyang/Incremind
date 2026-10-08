const assert = require("node:assert/strict");
const test = require("node:test");

const { deterministicSerial, normalizeSbom, validateSbom } = require("../generate-node-sbom.cjs");

test("SBOM normalization is deterministic and sorts components and dependency edges", () => {
  const input = {
    bomFormat: "CycloneDX",
    specVersion: "1.5",
    serialNumber: "urn:uuid:random",
    metadata: { timestamp: "now", component: { "bom-ref": "root" } },
    components: [{ "bom-ref": "z", name: "z" }, { "bom-ref": "a", name: "a" }],
    dependencies: [{ ref: "root", dependsOn: ["z", "a"] }, { ref: "z", dependsOn: [] }, { ref: "a", dependsOn: [] }],
  };
  const properties = {
    "chriptmas:lock:sha256": "a".repeat(64),
    "chriptmas:package:sha256": "b".repeat(64),
    "chriptmas:candidate:sidecar-content-set-sha256": "c".repeat(64),
    "chriptmas:build:node": "v25.2.1",
    "chriptmas:build:npm": "11.6.2",
    "chriptmas:build:platform": "win32/x64",
  };
  const first = normalizeSbom(input, properties, "stable identity");
  const second = normalizeSbom(input, properties, "stable identity");

  assert.deepEqual(first, second);
  assert.equal(first.serialNumber, deterministicSerial("stable identity"));
  assert.equal("timestamp" in first.metadata, false);
  assert.deepEqual(first.components.map((item) => item["bom-ref"]), ["a", "z"]);
  assert.deepEqual(first.dependencies[0].dependsOn, []);
  assert.doesNotThrow(() => validateSbom(first));
});

test("SBOM validation fails when a dependency target is absent", () => {
  const invalid = {
    bomFormat: "CycloneDX",
    specVersion: "1.5",
    metadata: {
      component: { "bom-ref": "root" },
      properties: [
        { name: "chriptmas:lock:sha256", value: "a" },
        { name: "chriptmas:package:sha256", value: "b" },
        { name: "chriptmas:candidate:sidecar-content-set-sha256", value: "c" },
        { name: "chriptmas:build:node", value: "node" },
        { name: "chriptmas:build:npm", value: "npm" },
        { name: "chriptmas:build:platform", value: "platform" },
      ],
    },
    components: [{ "bom-ref": "a" }],
    dependencies: [{ ref: "root", dependsOn: ["missing"] }],
  };
  assert.throws(() => validateSbom(invalid), /dependency_target_missing/);
});
