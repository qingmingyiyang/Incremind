const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

const ROOT = path.resolve(__dirname, "..");

function sha256File(filePath) {
  return crypto.createHash("sha256").update(fs.readFileSync(filePath)).digest("hex");
}

function deterministicSerial(identity) {
  const hex = crypto.createHash("sha256").update(identity, "utf8").digest("hex").slice(0, 32);
  return `urn:uuid:${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function normalizeSbom(sbom, properties, identity) {
  const normalized = structuredClone(sbom);
  normalized.serialNumber = deterministicSerial(identity);
  delete normalized.metadata.timestamp;
  normalized.metadata.properties = Object.entries(properties)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([name, value]) => ({ name, value: String(value) }));
  normalized.components = [...(normalized.components || [])].sort((left, right) =>
    String(left["bom-ref"] || left.purl || left.name).localeCompare(String(right["bom-ref"] || right.purl || right.name))
  );
  normalized.dependencies = [...(normalized.dependencies || [])]
    .map((dependency) => ({ ...dependency, dependsOn: [...(dependency.dependsOn || [])].sort() }))
    .sort((left, right) => String(left.ref).localeCompare(String(right.ref)));
  return normalized;
}

function validateSbom(sbom) {
  if (sbom.bomFormat !== "CycloneDX" || sbom.specVersion !== "1.5") throw new Error("node_sbom_schema_invalid");
  if (!Array.isArray(sbom.components) || !sbom.components.length) throw new Error("node_sbom_components_missing");
  const refs = new Set(sbom.components.map((component) => component["bom-ref"]));
  refs.add(sbom.metadata?.component?.["bom-ref"]);
  for (const dependency of sbom.dependencies || []) {
    if (!refs.has(dependency.ref)) throw new Error(`node_sbom_dependency_ref_missing:${dependency.ref}`);
    for (const target of dependency.dependsOn || []) {
      if (!refs.has(target)) throw new Error(`node_sbom_dependency_target_missing:${target}`);
    }
  }
  const propertyNames = new Set((sbom.metadata?.properties || []).map((item) => item.name));
  for (const required of [
    "chriptmas:lock:sha256",
    "chriptmas:package:sha256",
    "chriptmas:candidate:sidecar-content-set-sha256",
    "chriptmas:build:node",
    "chriptmas:build:npm",
    "chriptmas:build:platform",
  ]) {
    if (!propertyNames.has(required)) throw new Error(`node_sbom_property_missing:${required}`);
  }
}

function runNpm(args, options = {}) {
  const npmCli = path.join(path.dirname(process.execPath), "node_modules", "npm", "bin", "npm-cli.js");
  if (!fs.existsSync(npmCli)) throw new Error("node_sbom_npm_cli_missing");
  return execFileSync(process.execPath, [npmCli, ...args], {
    encoding: "utf8",
    windowsHide: true,
    ...options,
  });
}

function generateNodeSboms(root = ROOT) {
  const manifestPath = path.join(root, "apps", "desktop-electron", ".sidecar-stage", "sidecar-manifest.json");
  const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  const contentSetHash = manifest.content_set_sha256;
  if (!/^[0-9a-f]{64}$/.test(contentSetHash || "")) throw new Error("node_sbom_candidate_hash_invalid");
  const nodeVersion = process.version;
  const npmVersion = runNpm(["--version"]).trim();
  const outputRoot = path.join(root, "docs", "vNext-remediation", "sbom");
  fs.mkdirSync(outputRoot, { recursive: true });
  const results = [];
  for (const ecosystem of [
    { id: "frontend", root: path.join(root, "src", "frontend") },
    { id: "electron", root: path.join(root, "apps", "desktop-electron") },
  ]) {
    const lockPath = path.join(ecosystem.root, "package-lock.json");
    const packagePath = path.join(ecosystem.root, "package.json");
    const lockHash = sha256File(lockPath);
    const packageHash = sha256File(packagePath);
    const raw = runNpm(["sbom", "--sbom-format", "cyclonedx"], {
      cwd: ecosystem.root,
      maxBuffer: 32 * 1024 * 1024,
    });
    const identity = `${ecosystem.id}:${lockHash}:${packageHash}:${contentSetHash}:${nodeVersion}:${npmVersion}:${process.platform}:${process.arch}`;
    const sbom = normalizeSbom(JSON.parse(raw), {
      "chriptmas:build:node": nodeVersion,
      "chriptmas:build:npm": npmVersion,
      "chriptmas:build:platform": `${process.platform}/${process.arch}`,
      "chriptmas:candidate:sidecar-content-set-sha256": contentSetHash,
      "chriptmas:lock:sha256": lockHash,
      "chriptmas:package:sha256": packageHash,
    }, identity);
    validateSbom(sbom);
    const outputPath = path.join(outputRoot, `${ecosystem.id}.cdx.json`);
    fs.writeFileSync(outputPath, `${JSON.stringify(sbom, null, 2)}\n`, "utf8");
    results.push({ ecosystem: ecosystem.id, output_path: path.relative(root, outputPath).replaceAll("\\", "/"), components: sbom.components.length, sha256: sha256File(outputPath) });
  }
  return results;
}

if (require.main === module) {
  try { console.log(JSON.stringify(generateNodeSboms(), null, 2)); }
  catch (error) { console.error(`[generate-node-sbom] ${error.stack || error.message}`); process.exitCode = 1; }
}

module.exports = { deterministicSerial, generateNodeSboms, normalizeSbom, validateSbom };
